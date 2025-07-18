import os
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.history import FileHistory
from prompt_toolkit.lexers import PygmentsLexer
from prompt_toolkit.shortcuts import CompleteStyle, PromptSession, prompt
from prompt_toolkit.styles import Style
from pygments.lexers import MarkdownLexer, guess_lexer_for_filename
from pygments.token import Token
from pygments.util import ClassNotFound
from rich.console import Console
from rich.text import Text

from .dump import dump  # noqa: F401


class AutoCompleter(Completer):
    def __init__(self, root, rel_fnames, addable_rel_fnames, commands):
        self.commands = commands
        self.addable_rel_fnames = addable_rel_fnames
        self.rel_fnames = rel_fnames

        fname_to_rel_fnames = defaultdict(list)
        for rel_fname in addable_rel_fnames:
            fname = os.path.basename(rel_fname)
            if fname != rel_fname:
                fname_to_rel_fnames[fname].append(rel_fname)
        self.fname_to_rel_fnames = fname_to_rel_fnames

        self.words = set()

        for rel_fname in addable_rel_fnames:
            self.words.add(rel_fname)

        for rel_fname in rel_fnames:
            self.words.add(rel_fname)

            fname = os.path.join(root, rel_fname)
            try:
                with open(fname, "r") as f:
                    content = f.read()
            except FileNotFoundError:
                continue
            try:
                lexer = guess_lexer_for_filename(fname, content)
            except ClassNotFound:
                continue
            tokens = list(lexer.get_tokens(content))
            self.words.update(token[1] for token in tokens if token[0] in Token.Name)

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        words = text.split()
        if not words:
            return

        if text[0] == "/":
            if len(words) == 1 and not text[-1].isspace():
                candidates = self.commands.get_commands()
                candidates = [(cmd, cmd) for cmd in candidates]
            else:
                for completion in self.commands.get_command_completions(words[0][1:], words[-1]):
                    yield completion
                return
        else:
            candidates = self.words
            candidates.update(set(self.fname_to_rel_fnames))
            candidates = [(word, f"`{word}`") for word in candidates]

        last_word = words[-1]
        for word_match, word_insert in candidates:
            if word_match.lower().startswith(last_word.lower()):
                rel_fnames = self.fname_to_rel_fnames.get(word_match, [])
                if rel_fnames:
                    for rel_fname in rel_fnames:
                        yield Completion(f"`{rel_fname}`", start_position=-len(last_word), display=rel_fname)
                else:
                    yield Completion(word_insert, start_position=-len(last_word), display=word_match)


class InputOutput:
    num_error_outputs = 0
    num_user_asks = 0

    def __init__(
        self,
        pretty=True,
        yes=False,
        input_history_file=None,
        chat_history_file=None,
        input=None,
        output=None,
        user_input_color="blue",
        tool_output_color=None,
        tool_error_color="red",
        encoding="utf-8",
    ):
        no_color = os.environ.get("NO_COLOR")
        if no_color is not None and no_color != "":
            pretty = False

        self.user_input_color = user_input_color if pretty else None
        self.tool_output_color = tool_output_color if pretty else None
        self.tool_error_color = tool_error_color if pretty else None

        self.input = input
        self.output = output

        self.pretty = pretty
        if self.output:
            self.pretty = False

        self.yes = yes

        self.input_history_file = input_history_file
        if chat_history_file is not None:
            self.chat_history_file = Path(chat_history_file)
        else:
            self.chat_history_file = None

        self.encoding = encoding

        if pretty:
            self.console = Console()
        else:
            self.console = Console(force_terminal=False, no_color=True)

        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.append_chat_history(f"\n# bosskit chat started at {current_time}\n\n")

    def read_text(self, filename):
        try:
            with open(filename, "r", encoding=self.encoding) as f:
                return f.read()
        except (FileNotFoundError, UnicodeError) as e:
            self.tool_error(str(e))
            return

    def get_input(self, root, rel_fnames, addable_rel_fnames, commands):
        if self.pretty:
            style = dict(style=self.user_input_color) if self.user_input_color else dict()
            self.console.rule(**style)
        else:
            print()

        rel_fnames = list(rel_fnames)
        show = " ".join(rel_fnames)
        if len(show) > 10:
            show += "\n"
        show += "> "

        inp = ""
        multiline_input = False

        if self.user_input_color:
            style = Style.from_dict(
                {
                    "": self.user_input_color,
                    "pygments.literal.string": f"bold italic {self.user_input_color}",
                }
            )
        else:
            style = None

        while True:
            completer_instance = AutoCompleter(root, rel_fnames, addable_rel_fnames, commands)
            if multiline_input:
                show = ". "

            session_kwargs = {
                "message": show,
                "completer": completer_instance,
                "reserve_space_for_menu": 4,
                "complete_style": CompleteStyle.MULTI_COLUMN,
                "input": self.input,
                "output": self.output,
                "lexer": PygmentsLexer(MarkdownLexer),
            }
            if style:
                session_kwargs["style"] = style

            if self.input_history_file is not None:
                session_kwargs["history"] = FileHistory(self.input_history_file)

            session = PromptSession(**session_kwargs)
            line = session.prompt()

            if line and line[0] == "{" and not multiline_input:
                multiline_input = True
                inp += line[1:] + "\n"
                continue
            elif line and line[-1] == "}" and multiline_input:
                inp += line[:-1] + "\n"
                break
            elif multiline_input:
                inp += line + "\n"
            else:
                inp = line
                break

        print()
        self.user_input(inp)
        return inp

    def user_input(self, inp):
        prefix = "####"
        if inp:
            hist = inp.splitlines()
        else:
            hist = ["<blank>"]

        hist = f"  \n{prefix} ".join(hist)

        hist = f"""
{prefix} {hist}"""
        self.append_chat_history(hist, linebreak=True)

    # OUTPUT

    def ai_output(self, content):
        hist = "\n" + content.strip() + "\n\n"
        self.append_chat_history(hist)

    def confirm_ask(self, question, default="y"):
        self.num_user_asks += 1

        if self.yes is True:
            res = "yes"
        elif self.yes is False:
            res = "no"
        else:
            res = prompt(question + " ", default=default)

        hist = f"{question.strip()} {res.strip()}"
        self.append_chat_history(hist, linebreak=True, blockquote=True)
        if self.yes in (True, False):
            self.tool_output(hist)

        if not res or not res.strip():
            return
        return res.strip().lower().startswith("y")

    def prompt_ask(self, question, default=None):
        self.num_user_asks += 1

        if self.yes is True:
            res = "yes"
        elif self.yes is False:
            res = "no"
        else:
            res = prompt(question + " ", default=default)

        hist = f"{question.strip()} {res.strip()}"
        self.append_chat_history(hist, linebreak=True, blockquote=True)
        if self.yes in (True, False):
            self.tool_output(hist)

        return res

    def tool_error(self, message):
        self.num_error_outputs += 1

        if message.strip():
            hist = f"{message.strip()}"
            self.append_chat_history(hist, linebreak=True, blockquote=True)

        message = Text(message)
        style = dict(style=self.tool_error_color) if self.tool_error_color else dict()
        self.console.print(message, **style)

    def tool_output(self, *messages, log_only=False):
        if messages:
            hist = " ".join(messages)
            hist = f"{hist.strip()}"
            self.append_chat_history(hist, linebreak=True, blockquote=True)

        if not log_only:
            messages = list(map(Text, messages))
            style = dict(style=self.tool_output_color) if self.tool_output_color else dict()
            self.console.print(*messages, **style)

    def append_chat_history(self, text, linebreak=False, blockquote=False):
        if blockquote:
            text = text.strip()
            text = "> " + text
        if linebreak:
            text = text.rstrip()
            text = text + "  \n"
        if not text.endswith("\n"):
            text += "\n"
        if self.chat_history_file is not None:
            with self.chat_history_file.open("a") as f:
                f.write(text)
