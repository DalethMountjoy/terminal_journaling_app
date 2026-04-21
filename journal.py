#!/usr/bin/env python3
"""journal.py — a minimal terminal journaling app"""

import os
import re
import random
import subprocess
from datetime import date, datetime, timedelta
from pathlib import Path

import pyfiglet
from dotenv import load_dotenv
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
import readchar
from prompt_toolkit import Application, prompt as pt_prompt
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import ConditionalContainer, HSplit, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.lexers import PygmentsLexer
from prompt_toolkit.styles import Style
from pygments.lexers.markup import MarkdownLexer

# ── config ─────────────────────────────────────────────────────────────────────

load_dotenv(Path(__file__).parent / ".env")
JOURNAL_DIR = Path(os.getenv("JOURNAL_DIR", Path(__file__).parent))
ENTRIES_DIR = JOURNAL_DIR / "entries"
POETRY_DIR  = JOURNAL_DIR / "poetry"
PROMPTS_FILE = JOURNAL_DIR / "prompts.txt"
API_KEY = os.getenv("ANTHROPIC_API_KEY")
console = Console()

# ── data helpers ───────────────────────────────────────────────────────────────

def load_prompts():
    if not PROMPTS_FILE.exists():
        return ["What's on your mind today?"]
    return [l.strip() for l in PROMPTS_FILE.read_text().splitlines() if l.strip()]

def list_entries():
    if not ENTRIES_DIR.exists():
        return []
    return sorted(ENTRIES_DIR.glob("*.md"), key=lambda p: p.name, reverse=True)

def list_poetry():
    if not POETRY_DIR.exists():
        return []
    return sorted(POETRY_DIR.glob("*.md"), key=lambda p: p.name, reverse=True)

def get_streak():
    day, streak = date.today(), 0
    while True:
        day_str = day.strftime("%Y-%m-%d")
        if any(f.startswith(day_str) for f in os.listdir(ENTRIES_DIR)):
            streak += 1
            day -= timedelta(days=1)
        else:
            break
    return streak

def get_week_status():
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    files = os.listdir(ENTRIES_DIR)
    result = []
    for i in range(7):
        day = monday + timedelta(days=i)
        day_str = day.strftime("%Y-%m-%d")
        result.append((day.strftime("%a"), any(f.startswith(day_str) for f in files), day == today))
    return result

def get_on_this_day():
    today = date.today()
    this_md = today.strftime("%m-%d")
    if not ENTRIES_DIR.exists():
        return []
    matches = [p for p in ENTRIES_DIR.glob("*.md")
               if p.name[5:10] == this_md and int(p.name[:4]) < today.year]
    return sorted(matches, key=lambda p: p.name, reverse=True)

def file_preview(path):
    for line in path.read_text().splitlines():
        if line.strip() and not line.startswith("<!--"):
            return line.strip()[:60]
    return ""

def file_body(path):
    lines = [l for l in path.read_text().splitlines() if not l.startswith("<!--")]
    return "\n".join(lines).strip()

def fmt_ts(filename):
    """'2026-04-15_163045.md' → '2026-04-15  16:30'"""
    stem = filename[:-3]
    date_p, time_p = stem.split("_")
    return f"{date_p}  {time_p[:2]}:{time_p[2:4]}"

def save_entry(text, prompt=None):
    ENTRIES_DIR.mkdir(exist_ok=True)
    ts = datetime.now()
    path = ENTRIES_DIR / ts.strftime("%Y-%m-%d_%H%M%S.md")
    prompt_line = f"<!-- PROMPT: {prompt} -->" if prompt else "<!-- FREEWRITE -->"
    path.write_text(
        f"<!-- DATE: {ts.strftime('%Y-%m-%d %H:%M:%S')} -->\n"
        f"<!-- WORDS: {len(text.split())} -->\n"
        f"{prompt_line}\n\n"
        f"{text}"
    )
    return path

def save_poetry(text):
    POETRY_DIR.mkdir(exist_ok=True)
    ts = datetime.now()
    path = POETRY_DIR / ts.strftime("%Y-%m-%d_%H%M%S.md")
    path.write_text(
        f"<!-- DATE: {ts.strftime('%Y-%m-%d %H:%M:%S')} -->\n"
        f"<!-- WORDS: {len(text.split())} -->\n"
        f"<!-- POETRY -->\n\n"
        f"{text}"
    )
    return path

def git_backup():
    try:
        dirs = ["entries/"]
        if POETRY_DIR.exists():
            dirs.append("poetry/")
        subprocess.run(["git", "-C", str(JOURNAL_DIR), "add"] + dirs, check=True, capture_output=True)
        msg = f"journal: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        subprocess.run(["git", "-C", str(JOURNAL_DIR), "commit", "-m", msg], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(JOURNAL_DIR), "pull", "--rebase", "origin", "main"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(JOURNAL_DIR), "push", "origin", "main"], check=True, capture_output=True)
    except subprocess.CalledProcessError:
        console.print("\n[yellow]⚠ Git push failed. Entry saved locally.[/yellow]")

# ── editor ─────────────────────────────────────────────────────────────────────

BANNER = pyfiglet.figlet_format("Time to write", font="small_slant", width=200).strip()

def run_editor(initial_text=""):
    """Full-screen multiline editor. Returns text on Ctrl+S, None on Ctrl+Q.
    F1 shows markdown reference without losing the current text.
    F2 toggles the banner header.
    """
    current_text = initial_text
    header_visible = [True]
    banner_height = BANNER.count("\n") + 2

    while True:
        result = {"text": None, "saved": False, "confirming": False, "show_help": False}
        kb = KeyBindings()

        @kb.add("c-s")
        def _save(event):
            result["text"] = event.app.current_buffer.text
            result["saved"] = True
            event.app.exit()

        @kb.add("c-q")
        def _ask(event):
            result["confirming"] = True
            event.app.invalidate()

        @kb.add("y", filter=Condition(lambda: result["confirming"]))
        def _yes(event):
            result["confirming"] = False
            event.app.exit()

        @kb.add("<any>", filter=Condition(lambda: result["confirming"]))
        def _no(event):
            result["confirming"] = False
            event.app.invalidate()

        @kb.add("f2")
        def _toggle_header(event):
            header_visible[0] = not header_visible[0]
            event.app.invalidate()

        @kb.add("f1")
        def _help(event):
            result["text"] = event.app.current_buffer.text
            result["show_help"] = True
            event.app.exit()

        def status():
            if result["confirming"]:
                return [("class:confirm", "  Discard entry? y confirms, any other key cancels ")]
            return [("class:hint", "  Ctrl+S save · Ctrl+Q discard · F1 md ref · F2 toggle header ")]

        buf = Buffer(multiline=True, document=Document(current_text, cursor_position=len(current_text)))

        layout = Layout(HSplit([
            ConditionalContainer(
                Window(
                    FormattedTextControl([("class:prompt", BANNER + "\n")]),
                    height=banner_height,
                ),
                filter=Condition(lambda: header_visible[0]),
            ),
            Window(BufferControl(buf, lexer=PygmentsLexer(MarkdownLexer)), wrap_lines=True),
            Window(FormattedTextControl(status), height=1),
        ]))

        Application(
            layout=layout, key_bindings=kb, full_screen=True,
            style=Style.from_dict({
                "prompt": "ansiyellow",
                "hint": "fg:ansibrightblack",
                "confirm": "bold ansiyellow",
                "pygments.token.generic.heading": "bold ansiyellow",
                "pygments.token.generic.subheading": "bold ansiyellow",
                "pygments.token.generic.emph": "italic",
                "pygments.token.generic.strong": "bold",
                "pygments.token.literal.string": "fg:ansigreen",
                "pygments.token.name.tag": "fg:ansibrightblack",
                "pygments.token.comment": "fg:ansibrightblack italic",
                "pygments.token.punctuation": "fg:ansibrightblack",
            }),
        ).run()

        if result["show_help"]:
            current_text = result["text"] or ""
            show_markdown_ref()
        elif result["saved"]:
            return result["text"]
        else:
            return None

# ── home screen ────────────────────────────────────────────────────────────────

def show_home():
    console.clear()
    entries = list_entries()
    poetry  = list_poetry()
    total   = len(entries)
    streak  = get_streak()
    today_str   = date.today().strftime("%Y-%m-%d")
    today_count = sum(1 for p in entries if p.name.startswith(today_str))
    week = get_week_status()

    console.print()
    term_w = console.size.width
    banner_lines = BANNER.splitlines()
    max_w = max(len(l.rstrip()) for l in banner_lines)
    pad = " " * max(0, (term_w - max_w) // 2)
    console.print("[yellow]" + "\n".join(pad + l for l in banner_lines) + "[/yellow]")
    console.print()

    days  = "  ".join(f"[[{n}]]" if is_today else f" {n} " for n, _, is_today in week)
    marks = "  ".join("  [yellow]✓[/yellow]  " if has else "  ·  " for _, has, _ in week)
    console.print(days, justify="center")
    console.print(marks, justify="center")
    console.print()

    s = "day" if streak == 1 else "days"
    console.print(f"[yellow]{streak}[/yellow] {s} streak  ·  [dim]{total} total[/dim]", justify="center")
    console.print()

    if today_count:
        label = "entry" if today_count == 1 else "entries"
        console.print(f"[green]✓ {today_count} {label} today[/green]", justify="center")
        console.print()

    on_this_day = get_on_this_day()
    if on_this_day:
        p = on_this_day[0]
        year = p.name[:4]
        preview = file_preview(p)[:50]
        console.print(f"[dim]On this day in {year}:[/dim] {preview}", justify="center")
        console.print()

    console.print("[dim]\\[p] prompted write[/dim]", justify="center")
    console.print("[dim]\\[f] freewrite[/dim]", justify="center")
    console.print("[dim]\\[o] poetry[/dim]", justify="center")
    if entries:
        console.print("[dim]\\[v] view entries[/dim]", justify="center")
        console.print("[dim]\\[s] search entries[/dim]", justify="center")
    if poetry:
        console.print("[dim]\\[w] view poetry[/dim]", justify="center")
    console.print("[dim]\\[?] markdown reference[/dim]", justify="center")
    if API_KEY:
        console.print("[dim]\\[c] claude chat[/dim]", justify="center")
    console.print("[dim]\\[q] quit[/dim]", justify="center")
    console.print()

# ── writing flow ───────────────────────────────────────────────────────────────

def write_flow(prompted=False):
    prompt_text = random.choice(load_prompts()) if prompted else None
    now = datetime.now()
    if prompted:
        initial_text = f"> {prompt_text}\n\n"
    else:
        date_str = now.strftime("%A, %B ") + str(now.day)
        initial_text = f"# {date_str}\n\n"
    text = run_editor(initial_text=initial_text)
    if not text or not text.strip():
        return
    path = save_entry(text, prompt=prompt_text)
    words = len(text.split())
    console.clear()
    console.print(Panel(
        f"[green]Saved[/green]  ·  [yellow]{words} words[/yellow]\n[dim]{path.name}[/dim]",
        title="journal", border_style="dim",
    ))
    git_backup()
    console.print("\n[dim]  press any key to continue[/dim]")
    readchar.readkey()

def write_poetry():
    text = run_editor(initial_text="# ")
    if not text or not text.strip():
        return
    path = save_poetry(text)
    words = len(text.split())
    console.clear()
    console.print(Panel(
        f"[green]Saved[/green]  ·  [yellow]{words} words[/yellow]\n[dim]{path.name}[/dim]",
        title="poetry", border_style="dim",
    ))
    git_backup()
    console.print("\n[dim]  press any key to continue[/dim]")
    readchar.readkey()

# ── browser & viewer ───────────────────────────────────────────────────────────

def show_browser(paths, title="entries", previews=None):
    idx = 0
    while True:
        console.clear()
        height = console.size.height
        visible = max(1, height - 6)
        start = max(0, min(idx - visible // 2, len(paths) - visible))
        end = min(start + visible, len(paths))
        console.print()
        console.print(f"[bold]{title}[/bold]  [dim]↑↓ navigate  Enter open  q back[/dim]", justify="center")
        console.print()
        for i in range(start, end):
            p = paths[i]
            ts = fmt_ts(p.name)
            preview = previews[i] if previews else file_preview(p)
            cursor = "[yellow]›[/yellow] " if i == idx else "  "
            console.print(f"  {cursor}{ts}  [dim]{preview}[/dim]")
        console.print()
        key = readchar.readkey()
        if key == readchar.key.UP and idx > 0:
            idx -= 1
        elif key == readchar.key.DOWN and idx < len(paths) - 1:
            idx += 1
        elif key in (readchar.key.ENTER, "\r", "\n"):
            show_viewer(paths[idx])
        elif key == "q":
            break

def show_viewer(path):
    console.clear()
    console.print(Panel(
        Markdown(file_body(path)),
        title=f"[dim]{fmt_ts(path.name)}[/dim]",
        border_style="dim",
    ))
    console.print("[dim]  q to return[/dim]")
    while readchar.readkey() != "q":
        pass

# ── search ─────────────────────────────────────────────────────────────────────

def show_search():
    console.clear()
    try:
        query = pt_prompt("  search: ").strip()
    except (EOFError, KeyboardInterrupt):
        return
    if not query:
        return
    pattern = re.compile(re.escape(query), re.IGNORECASE)
    results = []
    for path in sorted(ENTRIES_DIR.glob("*.md"), key=lambda p: p.name, reverse=True):
        for line in path.read_text().splitlines():
            if line.startswith("<!--"):
                continue
            if pattern.search(line):
                results.append((path, line.strip()[:60]))
                break
    if not results:
        console.print(f"\n[dim]  no results for '{query}'[/dim]\n")
        console.print("[dim]  press any key[/dim]")
        readchar.readkey()
        return
    show_browser([r[0] for r in results], title=f"search: {query}", previews=[r[1] for r in results])

# ── markdown reference ─────────────────────────────────────────────────────────

MARKDOWN_REF = """\
## Text Formatting
`**text**` → **bold**

`*text*` → *italic*

`~~text~~` → ~~strikethrough~~

`` `text` `` → `inline code`

## Headings
`# Heading 1`

`## Heading 2`

`### Heading 3`

## Lists
`- item` or `* item` → unordered list

`1. item` → ordered list

Two leading spaces before `-` for nested items

## Blockquote
`> text` →

> good for quotes or reflections

## Other
`---` → horizontal rule (section break)

`[link text](url)` → hyperlink

Triple backticks on their own line open and close a code block
"""

def show_markdown_ref():
    console.clear()
    console.print(Panel(Markdown(MARKDOWN_REF), title="Markdown Reference", border_style="dim"))
    console.print("[dim]  q to return[/dim]")
    while readchar.readkey() != "q":
        pass

# ── claude chat ────────────────────────────────────────────────────────────────

def claude_chat():
    import anthropic
    client = anthropic.Anthropic(api_key=API_KEY)
    history = []
    console.clear()
    console.print(Panel(
        "[dim]Claude chat — empty line or 'exit' to return[/dim]",
        border_style="dim",
    ))
    while True:
        try:
            user_input = console.input("\n[yellow]you[/yellow]  ")
        except (EOFError, KeyboardInterrupt):
            break
        if not user_input.strip() or user_input.strip().lower() == "exit":
            break
        history.append({"role": "user", "content": user_input})
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system="You are a thoughtful journaling companion. Be concise.",
            messages=history,
        )
        reply = response.content[0].text
        history.append({"role": "assistant", "content": reply})
        console.print(f"\n[dim]claude[/dim]  {reply}")

# ── main ───────────────────────────────────────────────────────────────────────

def main():
    ENTRIES_DIR.mkdir(exist_ok=True)
    while True:
        show_home()
        key = readchar.readkey()
        entries = list_entries()
        poetry  = list_poetry()
        if key == "p":
            write_flow(prompted=True)
        elif key == "f":
            write_flow(prompted=False)
        elif key == "o":
            write_poetry()
        elif key == "v" and entries:
            show_browser(entries)
        elif key == "s" and entries:
            show_search()
        elif key == "w" and poetry:
            show_browser(poetry, "poetry")
        elif key == "?":
            show_markdown_ref()
        elif key == "c" and API_KEY:
            claude_chat()
        elif key == "q":
            console.clear()
            break

if __name__ == "__main__":
    main()
