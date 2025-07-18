import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import git
import tiktoken
from prompt_toolkit.completion import Completion

from bosskit import prompts, utils


class Commands:
    def __init__(self, io, coder):
        self.io = io
        self.coder = coder
        self.tokenizer = tiktoken.encoding_for_model(coder.main_model.name)

    def is_command(self, inp):
        if inp[0] == "/":
            return True

    def get_commands(self):
        commands = []
        for attr in dir(self):
            if attr.startswith("cmd_"):
                commands.append("/" + attr[4:])

        return commands

    def get_command_completions(self, cmd_name, partial):
        cmd_completions_method_name = f"completions_{cmd_name}"
        cmd_completions_method = getattr(self, cmd_completions_method_name, None)
        if cmd_completions_method:
            for completion in cmd_completions_method(partial):
                yield completion

    def do_run(self, cmd_name, args):
        cmd_method_name = f"cmd_{cmd_name}"
        cmd_method = getattr(self, cmd_method_name, None)
        if cmd_method:
            return cmd_method(args)
        else:
            self.io.tool_output(f"Error: Command {cmd_name} not found.")

    def run(self, inp):
        words = inp.strip().split()
        if not words:
            return

        first_word = words[0]
        rest_inp = inp[len(words[0]) :]

        all_commands = self.get_commands()
        matching_commands = [cmd for cmd in all_commands if cmd.startswith(first_word)]
        if len(matching_commands) == 1:
            return self.do_run(matching_commands[0][1:], rest_inp)
        elif len(matching_commands) > 1:
            self.io.tool_error(f"Ambiguous command: {', '.join(matching_commands)}")
        else:
            self.io.tool_error(f"Error: {first_word} is not a valid command.")

    # any method called cmd_xxx becomes a command automatically.
    # each one must take an args param.

    def cmd_commit(self, args):
        "Commit edits to the repo made outside the chat (commit message optional)"

        if not self.coder.repo:
            self.io.tool_error("No git repository found.")
            return

        if not self.coder.repo.is_dirty():
            self.io.tool_error("No more changes to commit.")
            return

        commit_message = args.strip()
        self.coder.commit(message=commit_message, which="repo_files")

    def cmd_clear(self, args):
        "Clear the chat history"

        self.coder.done_messages = []
        self.coder.cur_messages = []

    def cmd_tokens(self, args):
        "Report on the number of tokens used by the current chat context"

        res = []

        # system messages
        msgs = [
            dict(role="system", content=self.coder.gpt_prompts.main_system),
            dict(role="system", content=self.coder.gpt_prompts.system_reminder),
        ]
        tokens = len(self.tokenizer.encode(json.dumps(msgs)))
        res.append((tokens, "system messages", ""))

        # chat history
        msgs = self.coder.done_messages + self.coder.cur_messages
        if msgs:
            msgs = [dict(role="dummy", content=msg) for msg in msgs]
            msgs = json.dumps(msgs)
            tokens = len(self.tokenizer.encode(msgs))
            res.append((tokens, "chat history", "use /clear to clear"))

        # repo map
        other_files = set(self.coder.get_all_abs_files()) - set(self.coder.abs_fnames)
        if self.coder.repo_map:
            repo_content = self.coder.repo_map.get_repo_map(self.coder.abs_fnames, other_files)
            if repo_content:
                tokens = len(self.tokenizer.encode(repo_content))
                res.append((tokens, "repository map", "use --map-tokens to resize"))

        # files
        for fname in self.coder.abs_fnames:
            relative_fname = self.coder.get_rel_fname(fname)
            quoted = utils.quoted_file(fname, relative_fname)
            tokens = len(self.tokenizer.encode(quoted))
            res.append((tokens, f"{relative_fname}", "use /drop to drop from chat"))

        self.io.tool_output("Approximate context window usage, in tokens:")
        self.io.tool_output()

        width = 8

        def fmt(v):
            return format(int(v), ",").rjust(width)

        col_width = max(len(row[1]) for row in res)

        total = 0
        for tk, msg, tip in res:
            total += tk
            msg = msg.ljust(col_width)
            self.io.tool_output(f"{fmt(tk)} {msg} {tip}")

        self.io.tool_output("=" * width)
        self.io.tool_output(f"{fmt(total)} tokens total")

        limit = self.coder.main_model.max_context_tokens
        remaining = limit - total
        if remaining > 0:
            self.io.tool_output(f"{fmt(remaining)} tokens remaining in context window")
        else:
            self.io.tool_error(f"{fmt(remaining)} tokens remaining, window exhausted!")
        self.io.tool_output(f"{fmt(limit)} tokens max context window size")

    def cmd_undo(self, args):
        "Undo the last git commit if it was done by bosskit"
        if not self.coder.repo:
            self.io.tool_error("No git repository found.")
            return

        if self.coder.repo.is_dirty():
            self.io.tool_error("The repository has uncommitted changes. Please commit or stash them before" " undoing.")
            return

        local_head = self.coder.repo.git.rev_parse("HEAD")
        current_branch = self.coder.repo.active_branch.name
        try:
            remote_head = self.coder.repo.git.rev_parse(f"origin/{current_branch}")
            has_origin = True
        except git.exc.GitCommandError:
            has_origin = False

        if has_origin:
            if local_head == remote_head:
                self.io.tool_error("The last commit has already been pushed to the origin. Undoing is not" " possible.")
                return

        last_commit = self.coder.repo.head.commit
        if not last_commit.message.startswith("bosskit:") or last_commit.hexsha[:7] != self.coder.last_bosskit_commit_hash:
            self.io.tool_error("The last commit was not made by bosskit in this chat session.")
            return
        self.coder.repo.git.reset("--hard", "HEAD~1")
        self.io.tool_output(
            f"{last_commit.message.strip()}\n"
            f"The above commit {self.coder.last_bosskit_commit_hash} "
            "was reset and removed from git.\n"
        )

        if self.coder.main_model.send_undo_reply:
            return prompts.undo_command_reply

    def cmd_diff(self, args):
        "Display the diff of the last bosskit commit"
        if not self.coder.repo:
            self.io.tool_error("No git repository found.")
            return

        if not self.coder.last_bosskit_commit_hash:
            self.io.tool_error("No previous bosskit commit found.")
            return

        commits = f"{self.coder.last_bosskit_commit_hash}~1"
        diff = self.coder.get_diffs(commits, self.coder.last_bosskit_commit_hash)

        # don't use io.tool_output() because we don't want to log or further colorize
        print(diff)

    def completions_add(self, partial):
        files = set(self.coder.get_all_relative_files())
        files = files - set(self.coder.get_inchat_relative_files())
        for fname in files:
            if partial.lower() in fname.lower():
                yield Completion(fname, start_position=-len(partial))

    def cmd_add(self, args):
        "Add matching files to the chat session"

        added_fnames = []
        files = self.coder.get_all_relative_files()
        for word in args.split():
            matched_files = [file for file in files if word in file]

            if not matched_files:
                if self.coder.repo is not None:
                    create_file = self.io.confirm_ask(
                        (f"No files matched '{word}'. Do you want to create the file and add it" " to git?"),
                    )
                else:
                    create_file = self.io.confirm_ask(f"No files matched '{word}'. Do you want to create the file?")

                if create_file:
                    (Path(self.coder.root) / word).touch()

                    matched_files = [word]
                    if self.coder.repo is not None:
                        self.coder.repo.git.add(os.path.join(self.coder.root, word))
                        commit_message = f"bosskit: Created and added {word} to git."
                        self.coder.repo.git.commit("-m", commit_message, "--no-verify")
                else:
                    self.io.tool_error(f"No files matched '{word}'")

            for matched_file in matched_files:
                abs_file_path = os.path.abspath(os.path.join(self.coder.root, matched_file))
                if abs_file_path not in self.coder.abs_fnames:
                    self.coder.abs_fnames.add(abs_file_path)
                    self.io.tool_output(f"Added {matched_file} to the chat")
                    added_fnames.append(matched_file)
                else:
                    self.io.tool_error(f"{matched_file} is already in the chat")

        if not added_fnames:
            return

        # only reply if there's been some chatting since the last edit
        if not self.coder.cur_messages:
            return

        reply = prompts.added_files.format(fnames=", ".join(added_fnames))
        return reply

    def completions_drop(self, partial):
        files = self.coder.get_inchat_relative_files()

        for fname in files:
            if partial.lower() in fname.lower():
                yield Completion(fname, start_position=-len(partial))

    def cmd_drop(self, args):
        "Remove matching files from the chat session"

        if not args.strip():
            self.io.tool_output("Dropping all files from the chat session.")
            self.coder.abs_fnames = set()

        for word in args.split():
            matched_files = [
                file for file in self.coder.abs_fnames if word.lower() in os.path.relpath(file, self.coder.root).lower()
            ]
            if not matched_files:
                self.io.tool_error(f"No files matched '{word}'")

            for matched_file in matched_files:
                relative_fname = os.path.relpath(matched_file, self.coder.root)
                self.coder.abs_fnames.remove(matched_file)
                self.io.tool_output(f"Removed {relative_fname} from the chat")

    def cmd_run(self, args):
        "Run a shell command and optionally add the output to the chat"
        try:
            parsed_args = shlex.split(args)
            result = subprocess.run(parsed_args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            combined_output = result.stdout
        except Exception as e:
            self.io.tool_error(f"Error running command: {e}")

        self.io.tool_output(combined_output)

        if self.io.confirm_ask("Add the output to the chat?", default="y"):
            for line in combined_output.splitlines():
                self.io.tool_output(line, log_only=True)

            msg = prompts.run_output.format(
                command=args,
                output=combined_output,
            )
            return msg

    def cmd_exit(self, args):
        "Exit the application"
        sys.exit()

    def cmd_ls(self, args):
        "List all known files and those included in the chat session"

        files = self.coder.get_all_relative_files()

        other_files = []
        chat_files = []
        for file in files:
            abs_file_path = os.path.abspath(os.path.join(self.coder.root, file))
            if abs_file_path in self.coder.abs_fnames:
                chat_files.append(file)
            else:
                other_files.append(file)

        if chat_files:
            self.io.tool_output("Files in chat:\n")
        for file in chat_files:
            self.io.tool_output(f"  {file}")

        if other_files:
            self.io.tool_output("\nRepo files not in the chat:\n")
        for file in other_files:
            self.io.tool_output(f"  {file}")

    def cmd_help(self, args):
        "Show help about all commands"
        commands = sorted(self.get_commands())
        for cmd in commands:
            cmd_method_name = f"cmd_{cmd[1:]}"
            cmd_method = getattr(self, cmd_method_name, None)
            if cmd_method:
                description = cmd_method.__doc__
                self.io.tool_output(f"{cmd} {description}")
            else:
                self.io.tool_output(f"{cmd} No description available.")
