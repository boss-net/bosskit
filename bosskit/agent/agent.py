#!/usr/bin/env python

import os
import sys
import traceback
from pathlib import Path

import backoff
import git
import openai
import requests
from openai.error import RateLimitError
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown

from bosskit import diffs, editors, models, prompts, utils
from bosskit.commands import Commands
from bosskit.repomap import RepoMap

from ..dump import dump  # noqa: F401


class MissingAPIKeyError(ValueError):
    pass


class Coder:
    abs_fnames = None
    repo = None
    last_bosskit_commit_hash = None
    last_asked_for_commit_time = 0
    repo_map = None

    def check_model_availability(self, main_model):
        available_models = openai.Model.list()
        model_ids = [model.id for model in available_models["data"]]
        return main_model.name in model_ids

    def __init__(
        self,
        io,
        main_model=models.GPT4.name,
        fnames=None,
        pretty=True,
        show_diffs=False,
        auto_commits=True,
        dirty_commits=True,
        dry_run=False,
        map_tokens=1024,
        verbose=False,
        openai_api_key=None,
        openai_api_agent=None,
        assistant_output_color="blue",
    ):
        if not openai_api_key:
            raise MissingAPIKeyError("No OpenAI API key provided.")
        openai.api_key = openai_api_key
        openai.api_agent = openai_api_agent

        self.verbose = verbose
        self.abs_fnames = set()
        self.cur_messages = []
        self.done_messages = []

        self.io = io

        if not auto_commits:
            dirty_commits = False

        self.auto_commits = auto_commits
        self.dirty_commits = dirty_commits
        self.assistant_output_color = assistant_output_color

        self.dry_run = dry_run
        self.pretty = pretty

        if pretty:
            self.console = Console()
        else:
            self.console = Console(force_terminal=True, no_color=True)

        main_model = models.Model(main_model)
        if not main_model.always_available:
            if not self.check_model_availability(main_model):
                if main_model != models.GPT4:
                    self.io.tool_error(f"API key does not support {main_model.name}, falling back to" f" {models.GPT35_16k.name}")
                main_model = models.GPT35_16k

        self.main_model = main_model
        self.edit_format = self.main_model.edit_format

        if self.edit_format == "whole":
            self.gpt_prompts = editors.WholeFilePrompts()
        elif self.edit_format == "diff":
            self.gpt_prompts = editors.EditBlockPrompts()
        else:
            raise ValueError(f"Model {main_model} requesting unknown edit format {self.edit_format}")

        self.io.tool_output(f"Model: {main_model.name}")

        self.show_diffs = show_diffs

        self.commands = Commands(self.io, self)

        self.set_repo(fnames)

        if self.repo:
            rel_repo_dir = os.path.relpath(self.repo.git_dir, os.getcwd())
            self.io.tool_output(f"Git repo: {rel_repo_dir}")
        else:
            self.io.tool_output("Git repo: none")
            self.find_common_root()

        if main_model.use_repo_map:
            rm_io = io if self.verbose else None
            self.repo_map = RepoMap(
                map_tokens,
                self.root,
                self.main_model,
                rm_io,
                self.gpt_prompts.repo_content_prefix,
            )

            if self.repo_map.use_ctags:
                self.io.tool_output(f"Repo-map: universal-ctags using {map_tokens} tokens")
            elif not self.repo_map.has_ctags and map_tokens > 0:
                self.io.tool_output(f"Repo-map: basic using {map_tokens} tokens (universal-ctags not found)")
            else:
                self.io.tool_output("Repo-map: disabled because map_tokens == 0")
        else:
            self.io.tool_output("Repo-map: disabled for gpt-3.5")

        for fname in self.get_inchat_relative_files():
            self.io.tool_output(f"Added {fname} to the chat.")

    def find_common_root(self):
        if self.abs_fnames:
            common_prefix = os.path.commonpath(list(self.abs_fnames))
            self.root = os.path.dirname(common_prefix)
        else:
            self.root = os.getcwd()

    def set_repo(self, cmd_line_fnames):
        if not cmd_line_fnames:
            cmd_line_fnames = ["."]

        repo_paths = []
        for fname in cmd_line_fnames:
            fname = Path(fname)
            if not fname.exists():
                self.io.tool_output(f"Creating empty file {fname}")
                fname.parent.mkdir(parents=True, exist_ok=True)
                fname.touch()

            try:
                repo_path = git.Repo(fname, search_parent_directories=True).working_dir
                repo_paths.append(repo_path)
            except git.exc.InvalidGitRepositoryError:
                pass

            if fname.is_dir():
                continue

            fname = fname.resolve()
            self.abs_fnames.add(str(fname))

        num_repos = len(set(repo_paths))

        if num_repos == 0:
            return
        if num_repos > 1:
            self.io.tool_error("Files are in different git repos.")
            return

        # https://github.com/gitpython-developers/GitPython/issues/427
        repo = git.Repo(repo_paths.pop(), odbt=git.GitDB)

        self.root = repo.working_tree_dir

        new_files = []
        for fname in self.abs_fnames:
            relative_fname = self.get_rel_fname(fname)
            tracked_files = set(repo.git.ls_files().splitlines())
            if relative_fname not in tracked_files:
                new_files.append(relative_fname)

        if new_files:
            rel_repo_dir = os.path.relpath(repo.git_dir, os.getcwd())

            self.io.tool_output(f"Files not tracked in {rel_repo_dir}:")
            for fn in new_files:
                self.io.tool_output(f" - {fn}")
            if self.io.confirm_ask("Add them?"):
                for relative_fname in new_files:
                    repo.git.add(relative_fname)
                    self.io.tool_output(f"Added {relative_fname} to the git repo")
                show_files = ", ".join(new_files)
                commit_message = f"Added new files to the git repo: {show_files}"
                repo.git.commit("-m", commit_message, "--no-verify")
                commit_hash = repo.head.commit.hexsha[:7]
                self.io.tool_output(f"Commit {commit_hash} {commit_message}")
            else:
                self.io.tool_error("Skipped adding new files to the git repo.")
                return

        self.repo = repo

    def get_files_content(self, fnames=None):
        if not fnames:
            fnames = self.abs_fnames

        prompt = ""
        for fname in fnames:
            relative_fname = self.get_rel_fname(fname)
            prompt += utils.quoted_file(fname, relative_fname)
        return prompt

    def get_files_messages(self):
        all_content = ""
        if self.abs_fnames:
            files_content = self.gpt_prompts.files_content_prefix
            files_content += self.get_files_content()
        else:
            files_content = self.gpt_prompts.files_no_full_files

        all_content += files_content

        other_files = set(self.get_all_abs_files()) - set(self.abs_fnames)
        if self.repo_map:
            repo_content = self.repo_map.get_repo_map(self.abs_fnames, other_files)
            if repo_content:
                if all_content:
                    all_content += "\n"
                all_content += repo_content

        files_messages = [
            dict(role="user", content=all_content),
            dict(role="assistant", content="Ok."),
        ]
        if self.abs_fnames:
            files_messages += [
                dict(role="system", content=self.gpt_prompts.system_reminder),
            ]

        return files_messages

    def run(self):
        self.done_messages = []
        self.cur_messages = []

        self.num_control_c = 0

        while True:
            try:
                new_user_message = self.run_loop()
                while new_user_message:
                    new_user_message = self.send_new_user_message(new_user_message)

            except KeyboardInterrupt:
                self.num_control_c += 1
                if self.num_control_c >= 2:
                    break
                self.io.tool_error("^C again or /exit to quit")
            except EOFError:
                return

    def should_dirty_commit(self, inp):
        is_commit_command = inp and inp.startswith("/commit")
        if is_commit_command:
            return

        if not self.dirty_commits:
            return
        if not self.repo:
            return
        if not self.repo.is_dirty():
            return
        if self.last_asked_for_commit_time >= self.get_last_modified():
            return
        return True

    def move_back_cur_messages(self, message):
        self.done_messages += self.cur_messages
        if message:
            self.done_messages += [
                dict(role="user", content=message),
                dict(role="assistant", content="Ok."),
            ]
        self.cur_messages = []

    def run_loop(self):
        inp = self.io.get_input(
            self.root,
            self.get_inchat_relative_files(),
            self.get_addable_relative_files(),
            self.commands,
        )

        self.num_control_c = 0

        if self.should_dirty_commit(inp):
            self.commit(ask=True, which="repo_files")

            # files changed, move cur messages back behind the files messages
            self.move_back_cur_messages(self.gpt_prompts.files_content_local_edits)

            if inp.strip():
                self.io.tool_output("Use up-arrow to retry previous command:", inp)
            return

        if not inp:
            return

        if self.commands.is_command(inp):
            return self.commands.run(inp)

        self.check_for_file_mentions(inp)

        return self.send_new_user_message(inp)

    def send_new_user_message(self, inp):
        self.cur_messages += [
            dict(role="user", content=inp),
        ]

        main_sys = self.gpt_prompts.main_system
        if self.main_model.use_repo_map:
            main_sys += "\n" + self.gpt_prompts.system_reminder

        messages = [
            dict(role="system", content=main_sys),
        ]

        messages += self.done_messages

        messages += self.get_files_messages()
        messages += self.cur_messages

        if self.verbose:
            utils.show_messages(messages)

        content, interrupted = self.send(messages)
        if interrupted:
            self.io.tool_error("\n\n^C KeyboardInterrupt")
            content += "\n^C KeyboardInterrupt"

        self.io.tool_output()
        if interrupted:
            self.cur_messages += [dict(role="assistant", content=content)]
            return

        edited, edit_error = self.apply_updates(content)
        if edit_error:
            return edit_error

        if not (self.edit_format == "whole" and edited):
            # Don't add assistant messages to the history if they contain "edits"
            # from the "whole" edit format.
            # Because those edits are actually fully copies of the file!
            # That wastes too much context window.
            self.cur_messages += [dict(role="assistant", content=content)]
        else:
            self.cur_messages += [dict(role="assistant", content=self.gpt_prompts.redacted_edit_message)]

        if edited:
            if self.auto_commits:
                saved_message = self.auto_commit()
            else:
                saved_message = None
            self.move_back_cur_messages(saved_message)

        add_rel_files_message = self.check_for_file_mentions(content)
        if add_rel_files_message:
            return add_rel_files_message

    def auto_commit(self):
        res = self.commit(history=self.cur_messages, prefix="bosskit: ")
        if res:
            commit_hash, commit_message = res
            self.last_bosskit_commit_hash = commit_hash

            saved_message = self.gpt_prompts.files_content_gpt_edits.format(
                hash=commit_hash,
                message=commit_message,
            )
        else:
            # TODO: if not self.repo then the files_content_gpt_no_edits isn't appropriate
            self.io.tool_error("Warning: no changes found in tracked files.")
            saved_message = self.gpt_prompts.files_content_gpt_no_edits

        return saved_message

    def check_for_file_mentions(self, content):
        words = set(word for word in content.split())

        # drop sentence punctuation from the end
        words = set(word.rstrip(",.!;") for word in words)

        # strip away all kinds of quotes
        quotes = "".join(['"', "'", "`"])
        words = set(word.strip(quotes) for word in words)

        addable_rel_fnames = self.get_addable_relative_files()

        mentioned_rel_fnames = set()
        fname_to_rel_fnames = {}
        for rel_fname in addable_rel_fnames:
            fname = os.path.agentname(rel_fname)
            if fname not in fname_to_rel_fnames:
                fname_to_rel_fnames[fname] = []
            fname_to_rel_fnames[fname].append(rel_fname)

        for fname, rel_fnames in fname_to_rel_fnames.items():
            if len(rel_fnames) == 1 and fname in words:
                mentioned_rel_fnames.add(rel_fnames[0])
            else:
                for rel_fname in rel_fnames:
                    if rel_fname in words:
                        mentioned_rel_fnames.add(rel_fname)

        if not mentioned_rel_fnames:
            return

        for rel_fname in mentioned_rel_fnames:
            self.io.tool_output(rel_fname)

        if not self.io.confirm_ask("Add these files to the chat?"):
            return

        for rel_fname in mentioned_rel_fnames:
            self.abs_fnames.add(os.path.abspath(os.path.join(self.root, rel_fname)))

        return prompts.added_files.format(fnames=", ".join(mentioned_rel_fnames))

    @backoff.on_exception(
        backoff.expo,
        (RateLimitError, requests.exceptions.ConnectionError),
        max_tries=5,
        on_backoff=lambda details: print(f"Retry in {details['wait']} seconds."),
    )
    def send_with_retries(self, model, messages):
        return openai.ChatCompletion.create(
            model=model,
            messages=messages,
            temperature=0,
            stream=True,
        )

    def send(self, messages, model=None, silent=False):
        if not model:
            model = self.main_model.name

        self.resp = ""
        interrupted = False
        try:
            completion = self.send_with_retries(model, messages)
            self.show_send_output(completion, silent)
        except KeyboardInterrupt:
            interrupted = True

        if not silent:
            self.io.ai_output(self.resp)

        return self.resp, interrupted

    def show_send_output(self, completion, silent):
        live = None
        if self.pretty and not silent:
            live = Live(vertical_overflow="scroll")

        try:
            if live:
                live.start()

            for chunk in completion:
                if chunk.choices[0].finish_reason not in (None, "stop"):
                    assert False, "Exceeded context window!"

                try:
                    text = chunk.choices[0].delta.content
                    self.resp += text
                except AttributeError:
                    continue

                if silent:
                    continue

                if self.pretty:
                    show_resp = self.resp
                    if self.edit_format == "whole":
                        try:
                            show_resp = self.update_files_gpt35(self.resp, mode="diff")
                        except ValueError:
                            pass
                    md = Markdown(show_resp, style=self.assistant_output_color, code_theme="default")
                    live.update(md)
                else:
                    sys.stdout.write(text)
                    sys.stdout.flush()
        finally:
            if live:
                live.stop()

    def update_files_gpt35(self, content, mode="update"):
        edited = set()
        chat_files = self.get_inchat_relative_files()
        if not chat_files:
            if mode == "diff":
                return content
            return

        output = []
        lines = content.splitlines(keepends=True)
        fname = None
        new_lines = []
        for i, line in enumerate(lines):
            if line.startswith("```"):
                if fname:
                    # ending an existing block
                    full_path = os.path.abspath(os.path.join(self.root, fname))

                    if mode == "diff":
                        with open(full_path, "r") as f:
                            orig_lines = f.readlines()

                        show_diff = diffs.diff_partial_update(
                            orig_lines,
                            new_lines,
                            final=True,
                        ).splitlines()
                        output += show_diff
                    else:
                        new_lines = "".join(new_lines)
                        Path(full_path).write_text(new_lines)
                        edited.add(fname)

                    fname = None
                    new_lines = []
                    continue

                # starting a new block
                if i == 0:
                    raise ValueError("No filename provided before ``` block")

                fname = lines[i - 1].strip()
                if fname not in chat_files:
                    if len(chat_files) == 1:
                        fname = list(chat_files)[0]
                    else:
                        show_chat_files = " ".join(chat_files)
                        raise ValueError(f"{fname} is not one of: {show_chat_files}")

            elif fname:
                new_lines.append(line)
            else:
                output.append(line)

        if mode == "diff":
            if fname:
                # ending an existing block
                full_path = os.path.abspath(os.path.join(self.root, fname))

                if mode == "diff":
                    with open(full_path, "r") as f:
                        orig_lines = f.readlines()

                    show_diff = diffs.diff_partial_update(
                        orig_lines,
                        new_lines,
                    ).splitlines()
                    output += show_diff

            return "\n".join(output)

        if fname:
            raise ValueError("Started a ``` block without closing it")

        return edited

    def update_files_gpt4(self, content):
        # might raise ValueError for malformed ORIG/UPD blocks
        edits = list(utils.find_original_update_blocks(content))

        edited = set()
        for path, original, updated in edits:
            full_path = os.path.abspath(os.path.join(self.root, path))

            if full_path not in self.abs_fnames:
                if not Path(full_path).exists():
                    question = f"Allow creation of new file {path}?"  # noqa: E501
                else:
                    question = f"Allow edits to {path} which was not previously provided?"  # noqa: E501
                if not self.io.confirm_ask(question):
                    self.io.tool_error(f"Skipping edit to {path}")
                    continue

                if not Path(full_path).exists():
                    Path(full_path).parent.mkdir(parents=True, exist_ok=True)
                    Path(full_path).touch()

                self.abs_fnames.add(full_path)

                # Check if the file is already in the repo
                if self.repo:
                    tracked_files = set(self.repo.git.ls_files().splitlines())
                    relative_fname = self.get_rel_fname(full_path)
                    if relative_fname not in tracked_files and self.io.confirm_ask(f"Add {path} to git?"):
                        self.repo.git.add(full_path)

            edited.add(path)
            if utils.do_replace(full_path, original, updated, self.dry_run):
                if self.dry_run:
                    self.io.tool_output(f"Dry run, did not apply edit to {path}")
                else:
                    self.io.tool_output(f"Applied edit to {path}")
            else:
                self.io.tool_error(f"Failed to apply edit to {path}")

        return edited

    def get_context_from_history(self, history):
        context = ""
        if history:
            context += "# Context:\n"
            for msg in history:
                context += msg["role"].upper() + ": " + msg["content"] + "\n"
        return context

    def get_commit_message(self, diffs, context):
        if len(diffs) >= 4 * 1024 * 4:
            self.io.tool_error(f"Diff is too large for {models.GPT35.name} to generate a commit message.")
            return

        diffs = "# Diffs:\n" + diffs

        messages = [
            dict(role="system", content=prompts.commit_system),
            dict(role="user", content=context + diffs),
        ]

        try:
            commit_message, interrupted = self.send(
                messages,
                model=models.GPT35.name,
                silent=True,
            )
        except openai.error.InvalidRequestError:
            self.io.tool_error(f"Failed to generate commit message using {models.GPT35.name} due to an invalid" " request.")
            return

        commit_message = commit_message.strip()
        if commit_message and commit_message[0] == '"' and commit_message[-1] == '"':
            commit_message = commit_message[1:-1].strip()

        if interrupted:
            self.io.tool_error(f"Unable to get commit message from {models.GPT35.name}. Use /commit to try again.")
            return

        return commit_message

    def get_diffs(self, *args):
        if self.pretty:
            args = ["--color"] + list(args)

        diffs = self.repo.git.diff(*args)
        return diffs

    def commit(self, history=None, prefix=None, ask=False, message=None, which="chat_files"):
        repo = self.repo
        if not repo:
            return

        if not repo.is_dirty():
            return

        def get_dirty_files_and_diffs(file_list):
            diffs = ""
            relative_dirty_files = []
            for fname in file_list:
                relative_fname = self.get_rel_fname(fname)
                relative_dirty_files.append(relative_fname)

                try:
                    current_branch_commit_count = len(list(self.repo.iter_commits(self.repo.active_branch)))
                except git.exc.GitCommandError:
                    current_branch_commit_count = None

                if not current_branch_commit_count:
                    continue

                these_diffs = self.get_diffs("HEAD", "--", relative_fname)

                if these_diffs:
                    diffs += these_diffs + "\n"

            return relative_dirty_files, diffs

        if which == "repo_files":
            all_files = [os.path.join(self.root, f) for f in self.get_all_relative_files()]
            relative_dirty_fnames, diffs = get_dirty_files_and_diffs(all_files)
        elif which == "chat_files":
            relative_dirty_fnames, diffs = get_dirty_files_and_diffs(self.abs_fnames)
        else:
            raise ValueError(f"Invalid value for 'which': {which}")

        if self.show_diffs or ask:
            # don't use io.tool_output() because we don't want to log or further colorize
            print(diffs)

        context = self.get_context_from_history(history)
        if message:
            commit_message = message
        else:
            commit_message = self.get_commit_message(diffs, context)

        if not commit_message:
            commit_message = "work in progress"

        if prefix:
            commit_message = prefix + commit_message

        if ask:
            if which == "repo_files":
                self.io.tool_output("Git repo has uncommitted changes.")
            else:
                self.io.tool_output("Files have uncommitted changes.")

            res = self.io.prompt_ask(
                "Commit before the chat proceeds [y/n/commit message]?",
                default=commit_message,
            ).strip()
            self.last_asked_for_commit_time = self.get_last_modified()

            self.io.tool_output()

            if res.lower() in ["n", "no"]:
                self.io.tool_error("Skipped commmit.")
                return
            if res.lower() not in ["y", "yes"] and res:
                commit_message = res

        repo.git.add(*relative_dirty_fnames)

        full_commit_message = commit_message + "\n\n" + context
        repo.git.commit("-m", full_commit_message, "--no-verify")
        commit_hash = repo.head.commit.hexsha[:7]
        self.io.tool_output(f"Commit {commit_hash} {commit_message}")

        return commit_hash, commit_message

    def get_rel_fname(self, fname):
        return os.path.relpath(fname, self.root)

    def get_inchat_relative_files(self):
        files = [self.get_rel_fname(fname) for fname in self.abs_fnames]
        return sorted(set(files))

    def get_all_relative_files(self):
        if self.repo:
            files = self.repo.git.ls_files().splitlines()
        else:
            files = self.get_inchat_relative_files()

        return sorted(set(files))

    def get_all_abs_files(self):
        files = self.get_all_relative_files()
        files = [os.path.abspath(os.path.join(self.root, path)) for path in files]
        return files

    def get_last_modified(self):
        files = self.get_all_abs_files()
        if not files:
            return 0
        return max(Path(path).stat().st_mtime for path in files)

    def get_addable_relative_files(self):
        return set(self.get_all_relative_files()) - set(self.get_inchat_relative_files())

    def apply_updates(self, content):
        if self.edit_format == "diff":
            method = self.update_files_gpt4
        elif self.edit_format == "whole":
            method = self.update_files_gpt35
        else:
            raise ValueError(f"apply_updates() doesn't support {self.main_model.name}")

        try:
            edited = method(content)
            return edited, None
        except ValueError as err:
            err = err.args[0]
            self.io.tool_error("Malformed ORIGINAL/UPDATE blocks, retrying...")
            self.io.tool_error(str(err))
            return None, err

        except Exception as err:
            print(err)
            print()
            traceback.print_exc()
            return None, err
