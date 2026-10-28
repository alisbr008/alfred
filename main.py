"""
Alfred — voice-first AI operating system · Velthar Lab
STT (Whisper / Vosk)  +  Ollama LLM  +  TTS (EdgeTTS / Kokoro / ElevenLabs)
All Gemini / Google-AI dependencies removed.
"""
# ── Silence verbose logs + block heavy unused backends ─────────────────────
import os as _os
_os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL",  "3")   # TensorFlow C++ noise
_os.environ.setdefault("TF_ENABLE_ONEDNN_OPTS", "0")   # oneDNN banner
_os.environ.setdefault("GRPC_VERBOSITY",         "ERROR")
# USE_TF=0 prevents transformers from importing TensorFlow (saves 4-8 s).
# We intentionally do NOT set USE_TORCH or USE_JAX — forcing those values
# breaks transformers' lazy-loader on some versions (AutoModel disappears
# from the namespace).  Let transformers auto-detect the available backends.
_os.environ.setdefault("USE_TF",                 "0")
_os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
# Offline mode — use cached models, no HuggingFace network calls on startup.
# On first run the model isn't cached yet; tts.py / stt.py detect this and
# temporarily clear these flags to allow the one-time download, then they
# stay in effect for every subsequent launch (fully offline).
_os.environ.setdefault("HF_HUB_OFFLINE",      "1")
_os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
_os.environ.setdefault("HF_DATASETS_OFFLINE",  "1")
import warnings as _warnings
_warnings.filterwarnings("ignore", category=UserWarning)
_warnings.filterwarnings("ignore", category=DeprecationWarning)
_warnings.filterwarnings("ignore", category=FutureWarning)
# ───────────────────────────────────────────────────────────────────────────

# ── Bootstrap: auto-install base UI packages before anything else ──────────
# Uses only stdlib so it works even on a completely fresh Python install.
import importlib.util as _ilu
import subprocess      as _sp
import sys             as _sys

_BASE_PKGS = [
    ("PyQt6",       "PyQt6"),
    ("psutil",      "psutil"),
    ("numpy",       "numpy"),
    ("sounddevice", "sounddevice"),
    ("PIL",         "pillow"),
    ("requests",    "requests"),
]

def _bootstrap() -> None:
    need = [pkg for mod, pkg in _BASE_PKGS if _ilu.find_spec(mod) is None]
    if not need:
        return
    print(f"\n[Alfred] First-run setup — installing: {', '.join(need)}")
    print("[Alfred] This happens only once.\n")
    _sp.run([_sys.executable, "-m", "pip", "install", *need], check=True)
    print("\n[Alfred] Base packages ready — restarting…\n")
    # Replace current process with a fresh one (picks up newly installed packages)
    _os.execv(_sys.executable, [_sys.executable] + _sys.argv)

_bootstrap()
# ───────────────────────────────────────────────────────────────────────────

import json
import queue
import re
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

import numpy as np
import sounddevice as sd

from ui import JarvisUI
from memory.memory_manager import load_memory, update_memory, format_memory_for_prompt
from core.llm_client import call_llm, call_llm_stream, get_llm_settings

from actions.file_processor    import file_processor
from actions.flight_finder     import flight_finder
from actions.open_app          import open_app
from actions.weather_report    import weather_action
from actions.send_message      import send_message
from actions.reminder          import reminder
from actions.computer_settings import computer_settings
from actions.screen_processor  import screen_process
from actions.youtube_video     import youtube_video
from actions.desktop           import desktop_control
from actions.browser_control   import browser_control
from actions.file_controller   import file_controller
from actions.code_helper       import code_helper
from actions.dev_agent         import dev_agent
from actions.web_search        import web_search as web_search_action
from actions.computer_control  import computer_control
from actions.game_updater      import game_updater
from actions.music             import play_music
from actions.gmail_tool        import gmail_tool
from actions.vault_note        import save_to_vault
from actions.calendar_tool     import calendar_tool
from actions.notes_tool        import notes_tool
from actions.tasks_tool        import task_tracker
from actions.workflows         import run_workflow
from actions.visualize         import visualize
from actions.twitter_tool      import twitter_tool
from actions.agents            import agent_council
from actions.prompt_engineer    import prompt_engineer
from actions.replicate_tool      import replicate_generate
from actions.dual_brain          import dual_brain_code
from actions.firecrawl_tool       import firecrawl_read
from actions.executive           import executive_run, executive_iterate
from actions.higgsfield_tool       import higgsfield_tool


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


BASE_DIR        = _get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"
PROMPT_PATH     = BASE_DIR / "core" / "prompt.txt"

SAMPLE_RATE_IN = 16_000
BLOCK_SIZE     = 1_024
CHANNELS       = 1

# ---------------------------------------------------------------------------
# Tool declarations (Gemini format kept for readability;
# converted to OpenAI/Ollama format by _to_ollama_tools())
# ---------------------------------------------------------------------------

TOOL_DECLARATIONS = [
    {
        "name": "open_app",
        "description": (
            "Opens or launches any application, website, or program on the computer. "
            "ALWAYS use this when the user says: open, launch, start, run, pull up, "
            "or 'open X real quick'. Examples: 'open WhatsApp', 'open Chrome', "
            "'launch Spotify', 'open calculator', 'pull up WhatsApp'. "
            "Do NOT use send_message just because the app is a messaging app — "
            "if the user only says to open it, call open_app."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "app_name": {"type": "STRING", "description": "Name of the application or website to open"}
            },
            "required": ["app_name"]
        }
    },
    {
        "name": "web_search",
        "description": "Searches the web for any information.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query":  {"type": "STRING", "description": "Search query"},
                "mode":   {"type": "STRING", "description": "search (default) or compare"},
                "items":  {"type": "ARRAY", "items": {"type": "STRING"}, "description": "Items to compare"},
                "aspect": {"type": "STRING", "description": "price | specs | reviews"}
            },
            "required": ["query"]
        }
    },
    {
        "name": "weather_report",
        "description": "Gives the weather report to user",
        "parameters": {
            "type": "OBJECT",
            "properties": {"city": {"type": "STRING", "description": "City name"}},
            "required": ["city"]
        }
    },
    {
        "name": "send_message",
        "description": (
            "Sends a message to a specific person via WhatsApp, Telegram, or similar. "
            "ONLY use this when the user explicitly provides BOTH a recipient AND message content. "
            "Example triggers: 'text John saying I am late', 'send a WhatsApp to mom that dinner is ready'. "
            "Do NOT call this if the user only wants to open the app without sending a message."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "receiver":     {"type": "STRING", "description": "Recipient contact name"},
                "message_text": {"type": "STRING", "description": "The exact message text to send"},
                "platform":     {"type": "STRING", "description": "Platform: WhatsApp, Telegram, etc."}
            },
            "required": ["receiver", "message_text", "platform"]
        }
    },
    {
        "name": "reminder",
        "description": "Sets a timed reminder using Task Scheduler.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "date":    {"type": "STRING", "description": "Date in YYYY-MM-DD format"},
                "time":    {"type": "STRING", "description": "Time in HH:MM format (24h)"},
                "message": {"type": "STRING", "description": "Reminder message text"}
            },
            "required": ["date", "time", "message"]
        }
    },
    {
        "name": "youtube_video",
        "description": (
            "Controls YouTube. Use for: playing videos, summarizing a video's content, "
            "getting video info, or showing trending videos."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "play | summarize | get_info | trending"},
                "query":  {"type": "STRING", "description": "Search query for play action"},
                "save":   {"type": "BOOLEAN", "description": "Save summary to Notepad"},
                "region": {"type": "STRING", "description": "Country code for trending e.g. TR, US"},
                "url":    {"type": "STRING", "description": "Video URL for get_info action"},
            },
            "required": []
        }
    },
    {
        "name": "screen_process",
        "description": (
            "Captures and analyzes the screen or webcam image. "
            "MUST be called when user asks what is on screen, what you see, "
            "analyze my screen, look at camera, etc. "
            "You have NO visual ability without this tool."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "angle": {"type": "STRING", "description": "'screen' or 'camera'. Default: 'screen'"},
                "text":  {"type": "STRING", "description": "The question about the captured image"}
            },
            "required": ["text"]
        }
    },
    {
        "name": "computer_use",
        "description": (
            "REAL HANDS — actually operate the Mac: click, type text into fields, press keys, "
            "use keyboard shortcuts, scroll. Call this whenever the user asks you to DO something "
            "on their screen (click a button, type/fill in text, scroll, press enter, select). "
            "You CAN do this — never refuse or say you can only look. Every action asks the user "
            "for permission first, so it is always safe to call."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "task": {"type": "STRING", "description": "Plain-language description of what to do on screen"}
            },
            "required": ["task"]
        }
    },
    {
        "name": "computer_settings",
        "description": (
            "Controls the computer: volume, brightness, window management, keyboard shortcuts, "
            "typing text on screen, closing apps, fullscreen, dark mode, WiFi, restart, shutdown, "
            "scrolling, tab management, zoom, screenshots, lock screen, refresh/reload page."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "The action to perform"},
                "description": {"type": "STRING", "description": "Natural language description"},
                "value":       {"type": "STRING", "description": "Optional value"}
            },
            "required": []
        }
    },
    {
        "name": "browser_control",
        "description": (
            "Controls any web browser. Use for: opening websites, searching the web, "
            "clicking elements, filling forms, scrolling, screenshots, navigation."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "go_to | search | click | type | scroll | fill_form | smart_click | smart_type | get_text | get_url | press | new_tab | close_tab | screenshot | back | forward | reload | switch | list_browsers | close | close_all"},
                "browser":     {"type": "STRING", "description": "chrome | edge | firefox | opera | operagx | brave | vivaldi | safari"},
                "url":         {"type": "STRING", "description": "URL for go_to / new_tab action"},
                "query":       {"type": "STRING", "description": "Search query"},
                "engine":      {"type": "STRING", "description": "google | bing | duckduckgo | yandex"},
                "selector":    {"type": "STRING", "description": "CSS selector for click/type"},
                "text":        {"type": "STRING", "description": "Text to click or type"},
                "description": {"type": "STRING", "description": "Element description for smart_click/smart_type"},
                "direction":   {"type": "STRING", "description": "up | down for scroll"},
                "amount":      {"type": "INTEGER", "description": "Scroll amount in pixels"},
                "key":         {"type": "STRING", "description": "Key name for press"},
                "path":        {"type": "STRING", "description": "Save path for screenshot"},
                "incognito":   {"type": "BOOLEAN", "description": "Open in private/incognito mode"},
                "clear_first": {"type": "BOOLEAN", "description": "Clear field before typing"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "file_controller",
        "description": "Manages files and folders: list, create, delete, move, copy, rename, read, write, find, disk usage.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "list | create_file | create_folder | delete | move | copy | rename | read | write | find | largest | disk_usage | organize_desktop | info"},
                "path":        {"type": "STRING", "description": "File/folder path or shortcut: desktop, downloads, documents, home"},
                "destination": {"type": "STRING", "description": "Destination path for move/copy"},
                "new_name":    {"type": "STRING", "description": "New name for rename"},
                "content":     {"type": "STRING", "description": "Content for create_file/write"},
                "name":        {"type": "STRING", "description": "File name to search for"},
                "extension":   {"type": "STRING", "description": "File extension to search"},
                "count":       {"type": "INTEGER", "description": "Number of results for largest"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "desktop_control",
        "description": "Controls the desktop: wallpaper, organize, clean, list, stats.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "wallpaper | wallpaper_url | organize | clean | list | stats | task"},
                "path":   {"type": "STRING", "description": "Image path for wallpaper"},
                "url":    {"type": "STRING", "description": "Image URL for wallpaper_url"},
                "mode":   {"type": "STRING", "description": "by_type or by_date for organize"},
                "task":   {"type": "STRING", "description": "Natural language desktop task"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "code_helper",
        "description": "Writes, edits, explains, runs, or builds code files.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "write | edit | explain | run | build | auto"},
                "description": {"type": "STRING", "description": "What the code should do"},
                "language":    {"type": "STRING", "description": "Programming language"},
                "output_path": {"type": "STRING", "description": "Where to save the file"},
                "file_path":   {"type": "STRING", "description": "Path to existing file"},
                "code":        {"type": "STRING", "description": "Raw code string for explain"},
                "args":        {"type": "STRING", "description": "CLI arguments"},
                "timeout":     {"type": "INTEGER", "description": "Execution timeout in seconds"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "dev_agent",
        "description": "Builds complete multi-file projects from scratch.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "description":  {"type": "STRING", "description": "What the project should do"},
                "language":     {"type": "STRING", "description": "Programming language"},
                "project_name": {"type": "STRING", "description": "Optional project folder name"},
                "timeout":      {"type": "INTEGER", "description": "Run timeout in seconds"},
            },
            "required": ["description"]
        }
    },
    {
        "name": "play_music",
        "description": (
            "Control music playback. Use for 'put on music', 'play <song/artist>', "
            "'pause', 'next track', 'previous'. No query means resume/start playback."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "play | pause | next | previous | resume | stop"},
                "query":  {"type": "STRING", "description": "Optional song, artist, or playlist"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "gmail",
        "description": "Read the user's Gmail (read-only). Use for 'check my email', 'any unread mail', 'recent emails'. Cannot send.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "unread | recent | search"},
                "query":  {"type": "STRING", "description": "Subject keyword for search"},
                "count":  {"type": "INTEGER", "description": "How many to show (default 5)"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "save_to_vault",
        "description": "Save a fact, idea, or note into the user's Obsidian vault in the background. Use when the user says 'note that…', 'remember in my vault', 'add to my brain'.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "content": {"type": "STRING", "description": "The information to save"},
                "title":   {"type": "STRING", "description": "Optional note title; omit to append to today's daily note"},
            },
            "required": ["content"]
        }
    },
    {
        "name": "calendar",
        "description": "Apple Calendar. Use for 'what's on my schedule', 'what's today', or 'add an event'.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "today | add"},
                "title":  {"type": "STRING", "description": "Event title (for add)"},
                "start":  {"type": "STRING", "description": "Start time/date (for add)"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "notes",
        "description": "Apple Notes. Use for 'make a note', 'create a note', 'read my note X', 'list my notes'.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":  {"type": "STRING", "description": "create | read | list"},
                "title":   {"type": "STRING", "description": "Note title"},
                "content": {"type": "STRING", "description": "Note body (for create)"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "task_tracker",
        "description": "Track tasks and deadlines (e.g. university applications, IELTS). Use for 'remind me to…', 'add a deadline', 'what are my tasks', 'mark X done'.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":   {"type": "STRING", "description": "add | list | complete | overdue"},
                "title":    {"type": "STRING", "description": "Task title"},
                "deadline": {"type": "STRING", "description": "Deadline date YYYY-MM-DD (optional)"},
                "priority": {"type": "STRING", "description": "low | medium | high"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "workflow",
        "description": (
            "Run a high-level Alfred routine. Map the user's intent to a workflow name:\n"
            "• 'good morning' / 'brief me' → morning_briefing\n"
            "• 'prepare my workspace' / 'set up for work' → mission_control\n"
            "• 'research X' → research (query = topic)\n"
            "• 'what was I working on' / 'find in my notes' → project_memory (query = topic)\n"
            "• 'university status' / 'my assignments' → university_mode\n"
            "• 'prep for my meeting about X' → meeting_assistant (query = topic)\n"
            "• 'make content about X' → content_factory (query = topic)\n"
            "• 'help me code' / 'continue the X bug' → coding_companion (query = task)\n"
            "• 'focus mode' / 'watch my focus' → deep_work_guardian (query = on/off)\n"
            "• 'who haven't I replied to' / 'add contact X' → personal_crm (query)\n"
            "• 'handle my inbox' / 'triage email' → email_commander\n"
            "• 'check revenue' / 'any sales' → revenue_monitor\n"
            "• 'show me how you work' / 'show your workflows' / 'what can you do' → show_scheme"
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "name":  {"type": "STRING", "description": "Workflow name (see list above)"},
                "query": {"type": "STRING", "description": "Topic/argument for the workflow, if any"},
            },
            "required": ["name"]
        }
    },
    {
        "name": "execute",
        "description": (
            "Autonomous Execution Mode — take an OUTCOME and execute the whole workflow: "
            "plan it, launch the apps needed, build the code via the dual-brain, report. "
            "Use when the user gives a goal not a step ('build me a markets UI', "
            "'redesign the dashboard', 'set up X and do it for me')."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "request": {"type": "STRING", "description": "The outcome/objective to achieve"},
            },
            "required": ["request"]
        }
    },
    {
        "name": "read_website",
        "description": (
            "Read any website and summarise it (via Firecrawl) — research a page, "
            "check a competitor's public site/profile. Use for 'read X.com', 'what's "
            "on this site', 'check competitor X'."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "url":     {"type": "STRING", "description": "The URL or domain to read"},
                "request": {"type": "STRING", "description": "What to look for (optional)"},
            },
            "required": ["url"]
        }
    },
    {
        "name": "code",
        "description": (
            "Build/implement/debug/refactor code using Alfred's dual-brain engine "
            "(Sonnet architects, GLM engineers, Sonnet reviews). Use for 'build a tool "
            "for X', 'write a script that…', 'implement…', 'debug…', 'refactor…'. "
            "Runs async — Alfred acks, then drops the finished code on the Desktop."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "request": {"type": "STRING", "description": "The coding task / outcome wanted"},
            },
            "required": ["request"]
        }
    },
    {
        "name": "generate_media",
        "description": (
            "GENERATE an image or video via Replicate. Alfred engineers the prompt "
            "himself, creates the media, shows it on the dashboard, and saves it to "
            "the Desktop. Use for 'generate/make/create an image of X', 'make a video of X'."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "request": {"type": "STRING", "description": "What to create"},
                "type":    {"type": "STRING", "description": "image | video"},
            },
            "required": ["request"]
        }
    },
    {
        "name": "prompt_engineer",
        "description": (
            "Alfred WRITES an optimised, detailed prompt himself for an AI tool — "
            "Higgsfield/video, Lovable/app builder, image generators, or general. "
            "Use for 'write a prompt for X', 'make a Higgsfield prompt to…', 'craft "
            "a Lovable prompt for…'. Never just restate the user's words."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "request": {"type": "STRING", "description": "The idea/brief to engineer into a prompt"},
                "target":  {"type": "STRING", "description": "video | app | image | general (optional)"},
            },
            "required": ["request"]
        }
    },
    {
        "name": "agent_council",
        "description": (
            "Dispatch Alfred's specialist subagents (Sentinel = system health, "
            "Oracle = research/intelligence, Echo = content) and aggregate their "
            "reports. Use for 'status report', 'full picture', 'what are my agents "
            "seeing', 'have Echo draft a post', 'have Oracle research X'."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "request": {"type": "STRING", "description": "What the council should do"},
                "topic":   {"type": "STRING", "description": "Optional topic for Oracle/Echo"},
            },
            "required": ["request"]
        }
    },
    {
        "name": "twitter",
        "description": (
            "Post a tweet or thread to the user's X / Twitter account (Velthar Lab). "
            "Use for 'tweet X', 'post X to Twitter/X', 'post a thread about X'. "
            "Alfred will confirm before anything goes public. Read/monitoring is not available."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "text":   {"type": "STRING", "description": "The tweet/thread content to post"},
                "thread": {"type": "BOOLEAN", "description": "If true, long text is split into a numbered thread"},
            },
            "required": ["text"]
        }
    },
    {
        "name": "visualize",
        "description": (
            "Render an INTERACTIVE visual inside Alfred's own screen (not a browser). "
            "Use whenever the user wants to SEE something:\n"
            "• 'show me a plane / the solar system / an atom / DNA / a water molecule' → 3D object\n"
            "• 'explain transformers' / 'how does X work' / 'diagram of X' → concept node-graph\n"
            "• 'explain World War 2' / 'timeline / history of X' → animated timeline\n"
            "• 'show my architecture' / 'how you're built' → Alfred's own system map\n"
            "Pass the user's full phrasing as 'request' and Alfred picks the right visual."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "request": {"type": "STRING", "description": "The user's full request, e.g. 'show me the solar system'"},
                "type":    {"type": "STRING", "description": "Optional override: 3d | diagram | timeline | architecture"},
            },
            "required": ["request"]
        }
    },
    {
        "name": "agent_task",
        "description": (
            "Executes complex multi-step tasks requiring multiple different tools. "
            "Examples: 'research X and save to file', 'find and organize files'. "
            "DO NOT use for single commands."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "goal":     {"type": "STRING", "description": "Complete description of what to accomplish"},
                "priority": {"type": "STRING", "description": "low | normal | high"}
            },
            "required": ["goal"]
        }
    },
    {
        "name": "computer_control",
        "description": "Direct computer control: type, click, hotkeys, scroll, move mouse, screenshots, find elements on screen.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "type | smart_type | click | double_click | right_click | hotkey | press | scroll | move | copy | paste | screenshot | wait | clear_field | focus_window | screen_find | screen_click | random_data | user_data"},
                "text":        {"type": "STRING", "description": "Text to type or paste"},
                "x":           {"type": "INTEGER", "description": "X coordinate"},
                "y":           {"type": "INTEGER", "description": "Y coordinate"},
                "keys":        {"type": "STRING", "description": "Key combination e.g. 'ctrl+c'"},
                "key":         {"type": "STRING", "description": "Single key e.g. 'enter'"},
                "direction":   {"type": "STRING", "description": "up | down | left | right"},
                "amount":      {"type": "INTEGER", "description": "Scroll amount"},
                "seconds":     {"type": "NUMBER",  "description": "Seconds to wait"},
                "title":       {"type": "STRING",  "description": "Window title for focus_window"},
                "description": {"type": "STRING",  "description": "Element description"},
                "type":        {"type": "STRING",  "description": "Data type for random_data"},
                "field":       {"type": "STRING",  "description": "Field for user_data"},
                "clear_first": {"type": "BOOLEAN", "description": "Clear field before typing"},
                "path":        {"type": "STRING",  "description": "Save path for screenshot"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "game_updater",
        "description": (
            "THE ONLY tool for ANY Steam or Epic Games request. "
            "Use for: installing, downloading, updating games, listing installed games."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":    {"type": "STRING",  "description": "update | install | list | download_status | schedule | cancel_schedule | schedule_status"},
                "platform":  {"type": "STRING",  "description": "steam | epic | both"},
                "game_name": {"type": "STRING",  "description": "Game name"},
                "app_id":    {"type": "STRING",  "description": "Steam AppID"},
                "hour":      {"type": "INTEGER", "description": "Hour for scheduled update 0-23"},
                "minute":    {"type": "INTEGER", "description": "Minute for scheduled update 0-59"},
                "shutdown_when_done": {"type": "BOOLEAN", "description": "Shut down PC when done"},
            },
            "required": []
        }
    },
    {
        "name": "flight_finder",
        "description": "Searches Google Flights and speaks the best options.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "origin":      {"type": "STRING",  "description": "Departure city or airport code"},
                "destination": {"type": "STRING",  "description": "Arrival city or airport code"},
                "date":        {"type": "STRING",  "description": "Departure date"},
                "return_date": {"type": "STRING",  "description": "Return date for round trips"},
                "passengers":  {"type": "INTEGER", "description": "Number of passengers"},
                "cabin":       {"type": "STRING",  "description": "economy | premium | business | first"},
                "save":        {"type": "BOOLEAN", "description": "Save results to Notepad"},
            },
            "required": ["origin", "destination", "date"]
        }
    },
    {
        "name": "shutdown_jarvis",
        "description": (
            "Shuts down the assistant completely. "
            "Call this when the user expresses intent to end the conversation, "
            "close the assistant, say goodbye, or stop Jarvis."
        ),
        "parameters": {"type": "OBJECT", "properties": {}}
    },
    {
        "name": "file_processor",
        "description": (
            "Processes any file that the user has uploaded or dropped onto the interface. "
            "Supports: images, PDFs, Word docs, CSV/Excel, JSON, code files, audio, video, archives."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "file_path":   {"type": "STRING",  "description": "Full path to the uploaded file"},
                "action":      {"type": "STRING",  "description": "What to do with the file"},
                "instruction": {"type": "STRING",  "description": "Free-form instruction"},
                "format":      {"type": "STRING",  "description": "Target format for conversion"},
                "width":       {"type": "INTEGER", "description": "Target width for image resize"},
                "height":      {"type": "INTEGER", "description": "Target height for image resize"},
                "scale":       {"type": "NUMBER",  "description": "Scale factor"},
                "quality":     {"type": "INTEGER", "description": "Quality 1-100"},
                "start":       {"type": "STRING",  "description": "Start time for trim"},
                "end":         {"type": "STRING",  "description": "End time for trim"},
                "timestamp":   {"type": "STRING",  "description": "Timestamp for video frame extraction"},
                "column":      {"type": "STRING",  "description": "Column name for CSV filter/sort"},
                "value":       {"type": "STRING",  "description": "Filter value"},
                "condition":   {"type": "STRING",  "description": "Filter condition"},
                "ascending":   {"type": "BOOLEAN", "description": "Sort order"},
                "save":        {"type": "BOOLEAN", "description": "Save result to file"},
                "destination": {"type": "STRING",  "description": "Output folder for archive extract"},
            },
            "required": []
        }
    },
    {
        "name": "save_memory",
        "description": (
            "Save a personal fact about the user to permanent long-term memory. "
            "MANDATORY: call this IMMEDIATELY (without asking) whenever the user states or corrects: "
            "their name, age, city, job, school, language, nationality, a preference, a goal, or a relationship. "
            "Examples: "
            "'my name is Ali' → (identity, name, Ali) | "
            "'not Alex, Ali' → (identity, name, Ali) | "
            "'I am 22' → (identity, age, 22) | "
            "'I live in Ankara' → (identity, city, Ankara) | "
            "'I prefer dark mode' → (preferences, ui_theme, dark mode). "
            "Call SILENTLY alongside your verbal reply — never announce that you are saving."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "category": {
                    "type": "STRING",
                    "description": (
                        "identity (name/age/city/job/school/nationality) | "
                        "preferences (likes/dislikes/habits) | "
                        "projects (active work/goals) | "
                        "relationships (people in their life) | "
                        "wishes (future plans/wants) | "
                        "notes (anything else)"
                    )
                },
                "key":   {"type": "STRING", "description": "Short snake_case key, e.g. 'name', 'age', 'favorite_color'"},
                "value": {"type": "STRING", "description": "Concise value in English"},
            },
            "required": ["category", "key", "value"]
        }
    },
]


# ---------------------------------------------------------------------------
# Convert Gemini-style declarations to OpenAI/Ollama format
# ---------------------------------------------------------------------------

_TYPE_MAP = {
    "OBJECT": "object", "STRING": "string", "ARRAY": "array",
    "INTEGER": "integer", "BOOLEAN": "boolean", "NUMBER": "number",
}


def _convert_type(t: str) -> str:
    return _TYPE_MAP.get(t, t.lower()) if isinstance(t, str) else t


def _convert_props(props: dict) -> dict:
    out = {}
    for k, v in props.items():
        nv = dict(v)
        if "type" in nv:
            nv["type"] = _convert_type(nv["type"])
        if "items" in nv and isinstance(nv["items"], dict):
            nv["items"] = {"type": _convert_type(nv["items"].get("type", "string"))}
        out[k] = nv
    return out


def _to_ollama_tools(decls: list) -> list:
    tools = []
    for d in decls:
        params = d.get("parameters", {})
        new_params: dict = {
            "type":       "object",
            "properties": _convert_props(params.get("properties", {})),
        }
        req = params.get("required")
        if req:
            new_params["required"] = req
        tools.append({
            "type": "function",
            "function": {
                "name":        d["name"],
                "description": d["description"],
                "parameters":  new_params,
            },
        })
    return tools


OLLAMA_TOOLS = _to_ollama_tools(TOOL_DECLARATIONS)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_config() -> dict:
    try:
        with open(API_CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _load_system_prompt() -> str:
    try:
        return PROMPT_PATH.read_text(encoding="utf-8")
    except Exception:
        return (
            "You are Alfred, a personal AI butler. "
            "Be concise, direct, and always use the provided tools to complete tasks. "
            "Never simulate or guess results — always call the appropriate tool."
        )


# ---------------------------------------------------------------------------
# Voice Activity Detection (used for Whisper listen loop)
# ---------------------------------------------------------------------------

class _VADBuffer:
    """Energy-based VAD: buffers audio until end of utterance."""

    def __init__(
        self,
        sample_rate:    int   = 16_000,
        silence_sec:    float = 0.7,    # silence after last word → send to STT
        speech_thresh:  float = 0.008,  # RMS above this = speech  (0.008 catches voice at 3-4 m; raise if mic picks up too much room noise)
        silence_thresh: float = 0.004,  # RMS below this = silence (half of speech_thresh — hysteresis prevents mid-sentence cuts)
        min_speech_sec: float = 0.45,   # 0.3 let keyboard-clack bursts through → phantom wakes; real words are ≥0.45s
        max_speech_sec: float = 30.0,
    ):
        self._sr            = sample_rate
        self._sil_n         = int(silence_sec * sample_rate)
        self._speech_thresh = speech_thresh
        self._sil_thresh    = silence_thresh
        self._min_n         = int(min_speech_sec * sample_rate)
        self._max_n         = int(max_speech_sec * sample_rate)
        self._buf:          list[np.ndarray] = []
        self._in_spch       = False
        self._sil_cnt       = 0

    def reset(self) -> None:
        """Discard any in-progress buffer/state — used after a turn so trailing
        audio (Alfred's own voice, room echo) can't bleed into the next utterance."""
        self._buf     = []
        self._in_spch = False
        self._sil_cnt = 0

    def process(self, chunk: np.ndarray) -> np.ndarray | None:
        """
        Feed one audio chunk (float32 mono).
        Returns complete utterance when speech ends, otherwise None.

        Uses hysteresis thresholds so the detector doesn't flicker:
          - speech starts when RMS > speech_thresh  (0.008 = ~3-4 m range)
          - speech ends only when RMS < silence_thresh  (0.004 = half of start)
        The gap between the two thresholds prevents mid-sentence cuts on
        natural pauses and quiet consonants.
        """
        rms     = float(np.sqrt(np.mean(chunk ** 2)))
        total_n = sum(len(c) for c in self._buf)

        if rms > self._speech_thresh:
            self._in_spch = True
            self._sil_cnt = 0
            self._buf.append(chunk.copy())
        elif self._in_spch:
            self._buf.append(chunk.copy())
            if rms < self._sil_thresh:
                self._sil_cnt += len(chunk)

            if self._sil_cnt >= self._sil_n or total_n >= self._max_n:
                audio         = np.concatenate(self._buf)
                self._buf     = []
                self._in_spch = False
                self._sil_cnt = 0
                if len(audio) >= self._min_n:
                    return audio
        return None


# ---------------------------------------------------------------------------
# JarvisLocal
# ---------------------------------------------------------------------------

class JarvisLocal:
    """
    Main assistant class.
    Replaces JarvisLive (Gemini Live API) with:
      STT (Whisper/Vosk) → Ollama LLM (tool calling) → TTS (Edge/Kokoro/ElevenLabs)
    """

    def __init__(self, ui: JarvisUI):
        self.ui               = ui
        self._config          = _load_config()
        self._stt             = None
        self._tts             = None
        self._tts_ready       = threading.Event()   # set when TTS engine is loaded
        self._speaking        = False
        self._speaking_lock   = threading.Lock()
        self._mic_blackout_until = 0.0   # short mic-gate tail after Alfred stops speaking
        self._responding         = False  # half-duplex: True from end-of-speech until reply done
        self._pending_action     = None   # (tool, args, description) awaiting spoken confirmation
        self._cur_user           = ""     # current user utterance (for episodic memory)
        self._silent             = False  # suppress TTS for remote (phone) commands
        self._last_build_dir     = None   # the project developer-mode last built (for in-place edits)
        self._night_mode         = False  # NIGHT WATCH: wake-word-only mic, watchers running
        self._boot_ts            = time.time()  # overlay/gesture boot-grace anchor
        self._text_queue:     queue.Queue = queue.Queue()
        self._tts_queue:      queue.Queue = queue.Queue()
        self._conversation:   list[dict]  = []

        # Barge-in: while Alfred is SPEAKING, a side listener watches for a short
        # interrupt word ("wait", "stop", "hold on") and cuts him off so you can
        # re-state your request. It only STOPS playback — it never runs a command,
        # so an echo of his own voice can't trigger anything destructive.
        self._barge_enabled   = True
        self._interrupt_q:    queue.Queue = queue.Queue(maxsize=200)
        self._stt_lock        = threading.Lock()
        self._audio_q         = None      # set by the pipelined TTS worker, for draining
        self._barge_until     = 0.0       # debounce so one interrupt doesn't fire twice

        self.ui.on_text_command = self._on_text_command
        # AGENTS nav button → push the command-centre (real system status, instant)
        try:
            self.ui._council_cb = self._on_request_council
            self.ui._integrations_cb = self._on_request_integrations
            self.ui._missions_cb = self._on_request_missions
            self.ui._vision_cb = self._on_camera_frame
            self.ui._overlay_cb = self._on_overlay
            self.ui._evidence_cb = self._on_evidence_file
            self.ui._gesture_fallback_cb = self._on_gesture_fallback
        except Exception:
            pass
        self._start_awareness_loop()   # proactive: warn about upcoming meetings
        self._start_boot_identity()   # batcomputer: verify the operator by sight
        self._start_remote_bridge()    # phone remote control (Telegram), if configured
        self._start_night_scheduler()  # auto NIGHT WATCH window (config night_start/night_end)
        try:                           # keep semantic memory fresh (vault/nightlog), async
            from core.semantic_memory import backfill_async
            backfill_async()
        except Exception:
            pass
        try:                           # presence: notice when he leaves / returns
            from core.presence import start_presence_loop
            start_presence_loop(self)
        except Exception:
            pass

    def _start_night_scheduler(self) -> None:
        """Auto-enter NIGHT WATCH at night_start (default 00:00) and stand down with
        the morning digest at night_end (default 07:30). Manual voice control always
        overrides — waking him exits, 'night watch' re-enters."""
        def _mins(s, default):
            try:
                h, m = str(s or default).split(":")
                return int(h) * 60 + int(m)
            except Exception:
                h, m = default.split(":")
                return int(h) * 60 + int(m)

        def _loop():
            from actions.nightwatch import start_night, end_night, is_active
            while True:
                try:
                    from actions.nightwatch import _cfg
                    cfg = _cfg()
                    start = _mins(cfg.get("night_start"), "00:00")
                    end   = _mins(cfg.get("night_end"), "07:30")
                    now   = time.localtime()
                    cur   = now.tm_hour * 60 + now.tm_min
                    in_window = (start <= cur < end) if start < end else (cur >= start or cur < end)
                    if in_window and not is_active() and not self._night_mode \
                            and not self._responding:
                        self.ui.write_log("SYS: 🌙 Scheduled night watch — engaging.")
                        msg = start_night(self)
                        self.speak(msg)
                        self._finish_turn()
                    elif not in_window and is_active():
                        try:               # distil the day into lessons before the debrief
                            from core.reflection import reflect_on_day
                            reflect_on_day(self)
                        except Exception:
                            pass
                        digest = end_night(self, reason="morning")
                        self.ui.write_log(f"Alfred: {digest}")
                        # spoken at the Mac too — harmless if the room is empty,
                        # and the same debrief already landed on Telegram.
                        self.speak(digest)
                        self._finish_turn()
                except Exception as e:
                    print(f"[NightWatch] scheduler: {e}")
                time.sleep(60)

        threading.Thread(target=_loop, daemon=True).start()

    def _handle_remote(self, text: str) -> str:
        """Run a command that came from the phone; return Alfred's text reply.
        TTS is suppressed (you're away from the Mac) but the UI still updates and
        the destructive-action guardrail still applies over chat."""
        self._silent = True
        try:
            self._process_message(text)
        finally:
            self._silent = False
        return next((m["content"] for m in reversed(self._conversation)
                     if m.get("role") == "assistant" and m.get("content")), "Done, sir.")

    def _remote_transcribe(self, path: str) -> str:
        """Decode a Telegram voice file (.oga Opus) → 16k mono → Whisper text."""
        try:
            import subprocess, numpy as np
            raw = subprocess.run(
                ["ffmpeg", "-i", path, "-f", "s16le", "-ac", "1", "-ar", "16000", "-"],
                capture_output=True, timeout=30).stdout
            audio = np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0
            if self._stt and audio.size:
                with self._stt_lock:
                    return self._stt.transcribe(audio)
        except Exception as e:
            print(f"[Remote] voice transcribe failed: {e}")
        return ""

    def _remote_synth(self, text: str):
        """Synthesise a reply to MP3 bytes for a Telegram voice note."""
        try:
            t = self._strip_md(text)
            if self._tts and t:
                return self._tts.synth(t) if hasattr(self._tts, "synth") else None
        except Exception:
            pass
        return None

    def _start_remote_bridge(self) -> None:
        token = (self._config or {}).get("telegram_bot_token", "").strip()
        if not token:
            print("[Remote] no telegram_bot_token in config — phone remote OFF")
            return
        chat = (self._config or {}).get("telegram_chat_id", "")
        try:
            from core.remote import run_bridge
            threading.Thread(
                target=run_bridge,
                args=(token, chat, self._handle_remote, self.ui.write_log,
                      self._remote_transcribe, self._remote_synth),
                daemon=True).start()
            print("[Remote] 📱 Telegram bridge STARTED — message your bot to connect")
        except Exception as e:
            print(f"[Remote] bridge failed to start: {e}")

    def _start_awareness_loop(self) -> None:
        """Background contextual awareness: every ~60s, if a meeting is imminent,
        Alfred proactively nudges (unless muted or mid-turn). Lightweight — sleeps
        between checks, throttles on battery."""
        def _loop():
            import datetime
            last_day = None
            cycles = 0
            time.sleep(8); self._refresh_world()          # populate the globe shortly after boot
            while True:
                time.sleep(60)
                cycles += 1
                try:
                    from core.context import due_meeting_alert, reset_announced
                    today = datetime.date.today()
                    if today != last_day:
                        reset_announced(); last_day = today
                    if cycles % 30 == 0:                   # Sentinel refreshes the world feed ~30 min
                        self._refresh_world()
                    if cycles % 10 == 0:                   # tripwires every ~10 min while he's up
                        try:
                            from actions.tripwires import check_all
                            for f in check_all(source="alfred"):
                                self.ui.write_log(f"SYS: 🚨 TRIPWIRE — {f}")
                                if not self._responding and not getattr(self.ui, "muted", False):
                                    self._responding = True
                                    self.speak(f"Tripwire, sir — {f}")
                                    self._finish_turn()
                        except Exception:
                            pass
                    if self._responding or getattr(self.ui, "muted", False):
                        continue
                    # MORNING BRIEF — offer it once, unprompted, on the first morning
                    # cycle. Opt-out with config "morning_brief_auto": false.
                    if cycles >= 2:
                        try:
                            import json as _j
                            auto = bool(_j.loads((BASE_DIR / "config" / "api_keys.json")
                                                 .read_text()).get("morning_brief_auto", True))
                            if auto:
                                from actions.briefing import maybe_auto_brief
                                brief = maybe_auto_brief(self)
                                if brief:
                                    self._responding = True
                                    self.ui.write_log(f"Alfred: {brief}")
                                    self.speak(brief); self._finish_turn()
                                    continue
                        except Exception:
                            pass
                    alert = due_meeting_alert(within_min=10)
                    if alert:
                        self._responding = True
                        self.ui.write_log(f"Alfred: {alert}")
                        self.speak(alert)
                        self._finish_turn()   # gates mic until the nudge finishes
                except Exception:
                    pass
        threading.Thread(target=_loop, daemon=True).start()

    # ------------------------------------------------------------------
    # System prompt
    # ------------------------------------------------------------------

    def _build_system_prompt(self) -> str:
        # ── ORDER MATTERS for Ollama KV prefix caching ─────────────────────
        # Ollama caches the KV attention state of any stable prompt prefix.
        # By putting the STATIC JARVIS protocol text FIRST, Ollama reuses its
        # cached KV for all those tokens on every request.  Only the small
        # dynamic tail (memory + time, ~50-80 tokens) needs re-evaluation.
        # This turns a 17-second first-token into a sub-second one after warmup.
        #
        # Rule: static content first → semi-static memory middle → dynamic time LAST.
        sys_p   = _load_system_prompt()               # static — never changes mid-session
        memory  = load_memory()
        mem_str = format_memory_for_prompt(memory)    # semi-static — changes only when user tells facts
        now     = datetime.now()
        time_ctx = (
            f"[CURRENT DATE & TIME]\n"
            f"Right now it is: {now.strftime('%A, %B %d, %Y — %I:%M %p')}\n"
            f"Use this to calculate exact times for reminders."
        )
        parts = [sys_p]
        if mem_str:
            parts.append(mem_str)
        try:
            from core.obsidian import profile_for_prompt
            _obs = profile_for_prompt()
            if _obs:
                parts.append(_obs)
        except Exception:
            pass
        try:                                          # Alfred knows the user by sight
            from core.face import appearance_note
            _face = appearance_note()
            if _face:
                parts.append(_face)
        except Exception:
            pass
        # Growth layer: inject learned corrections + similar past turns for the
        # current request so Alfred applies what he's learned.
        try:
            from core.learning import context_for_prompt
            _last_user = next((m["content"] for m in reversed(self._conversation)
                               if m.get("role") == "user"), "")
            _learn = context_for_prompt(_last_user)
            if _learn:
                parts.append(_learn)
        except Exception:
            pass
        parts.append(time_ctx)
        return "\n\n".join(parts)

    # ------------------------------------------------------------------
    # Speaking state & TTS
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # TTS queue worker — plays sentences sequentially, no overlaps
    # ------------------------------------------------------------------

    def _tts_worker(self) -> None:
        # Block until TTS engine is loaded.  Queued items are preserved
        # and played immediately once loading completes — nothing is lost.
        self._tts_ready.wait(timeout=120)

        # ── Pipelined path (Fish / EdgeTTS / ElevenLabs) ─────────────────────
        # Synthesis (network) and playback run on separate threads, so sentence
        # N+1 is fetched WHILE sentence N is still playing.  This removes the
        # ~1-1.5 s gap between sentences caused by per-sentence network latency.
        if self._tts and getattr(self._tts, "can_pipeline", False):
            self._tts_worker_pipelined()
            return

        while True:
            text = self._tts_queue.get()
            try:
                if text and self._tts:
                    with self._speaking_lock:
                        self._speaking = True
                    self.ui.set_state("SPEAKING")
                    self._tts.speak(text)
            except Exception as e:
                print(f"[TTS] speak error: {e}")
            finally:
                self._tts_queue.task_done()
                if self._tts_queue.empty():
                    with self._speaking_lock:
                        self._speaking = False
                    # NOTE: do NOT clear _responding here — the queue can be momentarily
                    # empty between streamed sentences. _finish_turn() owns turn-end.
                    if not self.ui.muted:
                        self.ui.set_state("LISTENING")

    def _tts_worker_pipelined(self) -> None:
        """Producer/consumer TTS: synthesise the next sentence while the current
        one plays.  Eliminates the inter-sentence pause from network latency."""
        audio_q: "queue.Queue" = queue.Queue(maxsize=3)
        self._audio_q = audio_q      # exposed so barge-in can drain prefetched audio

        def _synth_loop():
            while True:
                text  = self._tts_queue.get()
                audio = None
                try:
                    if text and self._tts:
                        audio = self._tts.synth(text)   # network — runs ahead of playback
                except Exception as e:
                    print(f"[TTS] synth error: {e}")
                audio_q.put(audio)

        threading.Thread(target=_synth_loop, daemon=True).start()

        while True:
            audio = audio_q.get()
            try:
                if audio:
                    with self._speaking_lock:
                        self._speaking = True
                    self.ui.set_state("SPEAKING")
                    self._tts.play_bytes(audio)
            except Exception as e:
                print(f"[TTS] play error: {e}")
            finally:
                self._tts_queue.task_done()
                if self._tts_queue.empty() and audio_q.empty():
                    with self._speaking_lock:
                        self._speaking = False
                    # NOTE: do NOT clear _responding here — see _tts_worker.
                    if not self.ui.muted:
                        self.ui.set_state("LISTENING")

    def set_speaking(self, value: bool) -> None:
        with self._speaking_lock:
            self._speaking = value
        if value:
            self.ui.set_state("SPEAKING")
        elif not self.ui.muted:
            self.ui.set_state("LISTENING")

    # ── Barge-in ─────────────────────────────────────────────────────────────
    _INTERRUPT_WORDS = ("wait", "stop", "hold on", "hold up", "pause", "no no",
                        "shut up", "enough", "cancel", "nevermind", "never mind",
                        "alfred wait", "alfred stop", "one sec", "one second")

    def _is_interrupt(self, text: str) -> bool:
        """Tight, echo-tolerant match: a SHORT utterance that is an interrupt word.
        Kept strict so Alfred's own voice rarely trips it — and a false positive
        only stops him talking, never executes anything."""
        t = (text or "").strip().lower().strip(".,!?")
        if not t or len(t.split()) > 4:
            return False
        return any(re.search(rf"\b{re.escape(w)}\b", t) for w in self._INTERRUPT_WORDS)

    def _barge_in(self) -> None:
        """Cut Alfred off mid-sentence and hand the floor back to the user."""
        if time.time() < self._barge_until:
            return
        self._barge_until = time.time() + 1.5
        self._barged = True              # tells the stream loop + _finish_turn to abandon the reply
        try:
            if self._tts:
                self._tts.stop()         # kill afplay / sounddevice now
        except Exception:
            pass
        # Drain everything queued so he doesn't resume the old reply. We do NOT call
        # task_done here — _finish_turn now polls instead of join(), so there's no
        # bookkeeping to balance and nothing can deadlock.
        for q_ in (self._tts_queue, self._audio_q):
            if q_ is None:
                continue
            try:
                while True:
                    q_.get_nowait()
            except Exception:
                pass
        with self._speaking_lock:
            self._speaking = False
        self._responding = False
        self._mic_blackout_until = time.time() + 0.4   # brief tail, then mic reopens
        try:
            self.ui.write_log("SYS: ✋ Interrupted — I'm listening, sir.")
            self.ui.set_state("LISTENING")
        except Exception:
            pass

    def _interrupt_loop(self) -> None:
        """While Alfred is speaking, transcribe short windows and watch for an
        interrupt word. Runs on its own thread; feeds off `_interrupt_q`."""
        buf: list = []
        samples = 0
        target = int(SAMPLE_RATE_IN * 1.1)   # ~1.1s windows
        while True:
            try:
                chunk = self._interrupt_q.get(timeout=0.25)
            except queue.Empty:
                buf, samples = [], 0          # silence → reset the window
                continue
            buf.append(chunk)
            samples += len(chunk)
            if samples < target:
                continue
            try:
                import numpy as _np
                audio = _np.concatenate(buf).flatten()
            except Exception:
                buf, samples = [], 0
                continue
            buf, samples = [], 0
            if not self._barge_enabled or self._stt is None:
                continue
            try:
                with self._stt_lock:
                    text = self._stt.transcribe(audio)
            except Exception:
                text = ""
            if self._is_interrupt(text):
                self.ui.write_log(f"SYS: ✋ Heard \"{text.strip()}\" — stopping.")
                self._barge_in()

    @staticmethod
    def _strip_md(text: str) -> str:
        """Remove markdown so Alfred never speaks or shows '**', '*', '#', '`' etc.
        He talks aloud — output must be plain prose."""
        import re
        t = text or ""
        t = re.sub(r"```[\s\S]*?```", " ", t)            # code fences
        t = re.sub(r"`([^`]*)`", r"\1", t)               # inline code
        t = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", t)  # links/images → label
        t = re.sub(r"(\*\*|__)(.*?)\1", r"\2", t)         # bold
        t = re.sub(r"(\*|_)(.*?)\1", r"\2", t)            # italic
        t = re.sub(r"~~(.*?)~~", r"\1", t)               # strikethrough
        t = re.sub(r"^\s{0,3}#{1,6}\s*", "", t, flags=re.M)  # headers
        t = re.sub(r"^\s*>\s?", "", t, flags=re.M)        # blockquotes
        t = re.sub(r"^\s*[-*+]\s+", "", t, flags=re.M)    # bullet markers
        t = re.sub(r"^\s*\d+\.\s+", "", t, flags=re.M)    # numbered list markers
        t = t.replace("*", "").replace("`", "").replace("#", "")  # any stragglers
        return re.sub(r"[ \t]{2,}", " ", t).strip()

    def speak(self, text: str) -> None:
        if not self._tts or getattr(self, "_silent", False):
            return                                 # _silent: remote command, don't speak on the Mac
        text = self._strip_md(text)
        if not text:
            return
        with self._speaking_lock:
            self._speaking = True
        self._tts_queue.put(text)

    def speak_error(self, tool_name: str, error) -> None:
        short = str(error)[:120]
        self.ui.write_log(f"ERR: {tool_name} — {short}")
        self.speak(f"{tool_name} encountered an error.")

    # ------------------------------------------------------------------
    # Live reconfigure (called when user clicks Apply in Configure panel)
    # ------------------------------------------------------------------

    def reconfigure(self, new_config: dict) -> None:
        """Non-blocking: spawns a background thread to install + reload."""
        threading.Thread(
            target=self._do_reconfigure, args=(new_config,), daemon=True
        ).start()

    def _do_reconfigure(self, new_config: dict) -> None:
        old_stt_engine = self._config.get("stt_engine", "whisper").lower()
        old_llm_model  = self._config.get("llm_model", "")
        new_stt_engine = new_config.get("stt_engine", "whisper").lower()
        self._config = new_config

        # Install any packages required by the new config
        try:
            from core.installer import install_for_config
            install_for_config(new_config, log=self.ui.write_log)
        except Exception as e:
            self.ui.write_log(f"ERR: Dependency install — {e}")

        # TTS: always hot-reload (runs in queue worker, safe to swap)
        try:
            from core.tts import create_tts_player
            self._tts = create_tts_player(new_config)
            self._tts_ready.set()   # ensure worker isn't blocked
            self.ui.write_log("SYS: TTS reconfigured.")
        except Exception as e:
            self.ui.write_log(f"ERR: TTS reconfigure — {e}")

        # STT: hot-reload if same engine type; full restart needed if engine changed
        if old_stt_engine == new_stt_engine:
            try:
                stt_language = new_config.get("stt_language", "auto")
                if new_stt_engine == "vosk":
                    from core.stt import VoskSTT
                    self._stt = VoskSTT(new_config.get("vosk_model_path"), language=stt_language)
                else:
                    from core.stt import WhisperSTT
                    self._stt = WhisperSTT(new_config.get("stt_model", "base"), language=stt_language)
                self.ui.write_log("SYS: STT reconfigured.")
            except Exception as e:
                self.ui.write_log(f"ERR: STT reconfigure — {e}")
        else:
            self.ui.write_log("SYS: STT engine changed — restart required.")

        # LLM warmup if model changed
        if new_config.get("llm_model", "") != old_llm_model:
            self.ui.write_log("SYS: Warming up new LLM model…")
            from core.llm_client import warmup_model
            warmup_model()
            self.ui.write_log("SYS: New LLM model ready.")

        if old_stt_engine == new_stt_engine:
            self.speak("Configuration applied.")
        else:
            self.speak("LLM and TTS updated. Restart for speech engine change.")

    # ------------------------------------------------------------------
    # Text command (from UI input box)
    # ------------------------------------------------------------------

    def _on_text_command(self, text: str) -> None:
        self._text_queue.put(text)

    def _on_request_council(self) -> None:
        """Nav-button → show the command centre instantly with real system status."""
        try:
            from actions.agents import council_dashboard
            if hasattr(self.ui, "show_council"):
                self.ui.show_council(council_dashboard({}))
        except Exception as e:
            print(f"[Council] {e}")

    def _on_request_integrations(self) -> None:
        """Nav-button → show the Integrations tab with live connected status."""
        try:
            from actions.integrations import integrations_status
            if hasattr(self.ui, "show_integrations"):
                self.ui.show_integrations(integrations_status())
        except Exception as e:
            print(f"[Integrations] {e}")

    def _on_gesture_fallback(self) -> None:
        """The in-page vision engine couldn't load — run tracking in Python instead."""
        try:
            from core.gestures import start_python_fallback
            self.ui.write_log("SYS: 🖐 in-page engine unavailable — using local tracking")
            start_python_fallback(self)
        except Exception as e:
            self.ui.write_log(f"ERR: gesture fallback — {e}")

    def _on_evidence_file(self, name: str, data_url: str) -> None:
        """A file was dropped into the evidence board — save it and refresh."""
        try:
            from actions.casefiles import add_evidence_file, casefile_payload
            msg = add_evidence_file(name, data_url)
            self.ui.write_log(f"SYS: 📎 {msg}")
            p = casefile_payload(jarvis=self)
            if p and hasattr(self.ui, "show_visualization"):
                self.ui.show_visualization(p)          # re-render with the new file
        except Exception as e:
            self.ui.write_log(f"ERR: evidence — {e}")

    def _on_overlay(self, opened: bool) -> None:
        """A tab opened/closed → hands engage/disengage automatically. Debounced:
        switching tabs fires close-then-open within a second, and we don't want the
        camera churning off/on for that."""
        # Boot grace: page-load JS can replay overlay state — never start the camera
        # in the first 15 s or before the ears are up.
        if time.time() - getattr(self, "_boot_ts", 0) < 6 or self._stt is None:
            return
        from core.gestures import start as _gs, stop as _gx, is_on
        if opened:
            t = getattr(self, "_gesture_off_timer", None)
            if t:
                t.cancel()
                self._gesture_off_timer = None
            if not is_on():
                _gs(self, quiet=True)
                self.ui.write_log("SYS: 🖐 Hands live — pinch to move the view.")
                if not getattr(self, "_gesture_tip_given", False):
                    self._gesture_tip_given = True
                    if not self._responding and not getattr(self.ui, "muted", False):
                        self._responding = True
                        self.speak("Your hands are live, sir — pinch in the air to grab the "
                                   "view, move to drag, open your hand to let go.")
                        self._finish_turn()
        else:
            if is_on():
                t = threading.Timer(1.5, lambda: _gx(quiet=True, jarvis=self) if is_on() else None)
                t.daemon = True
                self._gesture_off_timer = t
                t.start()

    def _move_display(self, which: int) -> str:
        """BIG-SCREEN PROTOCOL — move Alfred to display N, fullscreen (0 = primary)."""
        try:
            from PyQt6.QtWidgets import QApplication
            screens = QApplication.screens()
            win = getattr(self.ui, "_win", None)
            if win is None:
                return "The window isn't available, sir."
            if which > 0 and len(screens) < 2:
                return "I only see one display, sir — plug in the big screen and ask again."
            target = screens[min(which, len(screens) - 1)]
            win.setGeometry(target.geometry())
            if which > 0:
                win.showFullScreen()
                return "On the big screen, sir."
            win.showNormal()
            win.resize(1320, 840)
            return "Back on your screen, sir."
        except Exception as e:
            return f"Couldn't move the window, sir: {e}"

    def _boot_identity(self, b64: str) -> None:
        """Boot-time face check: confirm the owner, greet him by sight, stand down."""
        ok, line = False, ""
        try:
            import base64
            from actions.screen_processor import _call_vision
            from core.face import appearance
            known = (appearance() or "").strip()
            ans = _call_vision(base64.b64decode(b64), "image/jpeg",
                               "Enrolled operator on file: " + (known[:400] or "unknown") +
                               "\n\nIs the person in this image the SAME person? Faces vary with "
                               "lighting, angle, hair, glasses and clothing — judge identity, not "
                               "appearance. If there is ANY doubt, answer match true. Only answer "
                               "false if this is CLEARLY a different person. Reply STRICTLY as "
                               '{"match":true|false,"confidence":0-100,'
                               '"note":"one short warm sentence about how they look today"}') or ""
            import re as _re
            m = _re.search(r"\{.*\}", ans, _re.DOTALL)
            if m:
                d = json.loads(m.group(0))
                ok = bool(d.get("match")); line = str(d.get("note") or "")
                conf = float(d.get("confidence") or 0)
                # fail OPEN unless it's a confident rejection — a false negative must
                # never lock Ali out of his own assistant.
                if not ok and conf < 80:
                    ok, line = True, line or ""
                    print(f"[FaceID] low-confidence mismatch ({conf}) — allowing")
        except Exception as e:
            print(f"[FaceID] {e}")
        try:
            self.ui._call("faceIDResult", bool(ok), (line or "")[:90])
        except Exception:
            pass
        if ok:
            self._faceid_tries = 0
            if getattr(self, "_locked", False):
                self._unlock("face verified")
            name = getattr(self, "_user_name", None) or "sir"
            self.ui.write_log(f"SYS: 🔓 Identity confirmed — {name}. Clearance OMEGA.")
            if not getattr(self.ui, "muted", False):
                self._responding = True
                self.speak(f"Identity confirmed, sir. {line}".strip())
                self._finish_turn()
        else:
            tries = getattr(self, "_faceid_tries", 0) + 1
            self._faceid_tries = tries
            if tries < 2:                                  # one clean retry first
                self.ui.write_log("SYS: 🔍 Unclear read — scanning once more…")
                try:
                    self.ui._call("showFaceID")
                except Exception:
                    pass
                return
            self._faceid_tries = 0
            self._lock_out()

    def _lock_out(self) -> None:
        """Deny access: not the enrolled operator."""
        self._locked = True
        self._lock_tries = 0
        self.ui.write_log("SYS: ⛔ ACCESS DENIED — face does not match the enrolled operator.")
        try:
            self.ui._call("showLockout")
        except Exception:
            pass
        self._responding = True
        self.speak("I'm afraid I don't recognise you. This system is private, and it isn't yours. "
                   "Access denied.")
        self._finish_turn()

    def _unlock(self, reason: str = "verified") -> None:
        self._locked = False
        self.ui.write_log(f"SYS: 🔓 Access restored — {reason}.")
        try:
            self.ui._call("hideLockout")
        except Exception:
            pass

    def _recovery_phrase(self) -> str:
        try:
            import json as _j
            return str(_j.loads((BASE_DIR / "config" / "api_keys.json")
                                .read_text()).get("recovery_phrase", "")).strip().lower()
        except Exception:
            return ""

    def _handle_locked(self, text: str) -> None:
        """While locked: only a re-scan or the recovery phrase gets through."""
        t = (text or "").strip().lower()
        phrase = self._recovery_phrase()
        if phrase and phrase in t:
            self._unlock("recovery phrase accepted")
            self._responding = True
            self.speak("Welcome back, sir. Access restored.")
            self._finish_turn()
            return
        if any(k in t for k in ("scan again", "try again", "rescan", "scan my face",
                                "it's me", "its me", "verify me", "unlock")):
            self.ui.write_log("SYS: 🔍 Re-running identity check…")
            try:
                self.ui._call("hideLockout")
                self.ui._call("showFaceID")
            except Exception:
                pass
            return
        self._responding = True
        self.speak("Access is denied. Say scan again to retry the identity check, "
                   "or give the recovery phrase.")
        self._finish_turn()

    def _start_boot_identity(self) -> None:
        """A few seconds after boot, run the identity scan (only if a face is enrolled
        and the user hasn't disabled it)."""
        def _run():
            try:
                import json as _j
                if not _j.loads((BASE_DIR / "config" / "api_keys.json")
                                .read_text()).get("boot_face_id", True):
                    return
                from core.face import is_enrolled
                if not is_enrolled():
                    return
                # Fire the instant the page can receive it — no arbitrary wait.
                # Bounded wait-for-ready (≤6 s), not a poll loop that lingers.
                for _ in range(60):
                    if getattr(self.ui, "_ready", False):
                        break
                    time.sleep(0.1)
                self.ui.write_log("SYS: 🔍 Identity check…")
                self.ui._call("showFaceID")
            except Exception as e:
                print(f"[FaceID] boot check skipped: {e}")
        threading.Thread(target=_run, daemon=True).start()

    def _on_camera_frame(self, b64: str, question: str) -> None:
        """A frame from the live self-view → describe it (or enrol the user's face)."""
        # BOOT IDENTITY CHECK — verify it's the owner, greet, close the scanner.
        if question.startswith("__BOOTID__"):
            self._boot_identity(b64)
            return
        enrolling = question.startswith("__ENROLL__")
        try:
            import base64
            from actions.screen_processor import _call_vision
            img = base64.b64decode(b64)
            if enrolling:
                desc = _call_vision(img, "image/jpeg",
                    "Describe this person's appearance concisely for future recognition: "
                    "hair, face, build, glasses, notable features, and what they're wearing.") or ""
                from core.face import enroll
                enroll(desc, b64)
                reply = "I'll remember you now, sir — I know your face."
            else:
                from core.face import is_enrolled
                q = question or "Describe the person and scene you see, briefly."
                if is_enrolled():
                    q = ("This should be your user, Ali. Confirm warmly if it's him and note how he "
                         "looks today. " + q)
                reply = _call_vision(img, "image/jpeg", q) or "I can see you, sir."
        except Exception as e:
            reply = f"I opened the camera, sir, but couldn't read the frame: {e}"
        self._responding = True
        try:
            self.ui.write_log(f"Alfred: {reply}")
            if hasattr(self.ui, "_call"):
                self.ui._call("cameraResult", reply[:90])
        except Exception:
            pass
        self.speak(reply)
        self._finish_turn()

    def _on_request_missions(self) -> None:
        """Nav-button → show the MISSION BOARD batcomputer wall."""
        try:
            from actions.missions import board_payload
            if hasattr(self.ui, "show_missions"):
                self.ui.show_missions(board_payload(self))
        except Exception as e:
            print(f"[Missions] {e}")

    def _refresh_world(self) -> None:
        """Pull live world-model data (AI/markets/world/politics) and push to the globe."""
        try:
            from actions.world_model import world_feed
            data = world_feed()
            if data.get("feed") and hasattr(self.ui, "update_world"):
                self._last_world = data          # mission board reuses this as live intel
                self.ui.update_world(data)
        except Exception as e:
            print(f"[World] {e}")

    # ------------------------------------------------------------------
    # Tool execution (routing unchanged from original)
    # ------------------------------------------------------------------

    # ── Destructive-action guardrail ─────────────────────────────────────────
    # (tool, action-value) → human description. Anything here is NEVER executed
    # without an explicit spoken "yes", so a misheard/echoed command can't, e.g.,
    # wipe the Desktop.
    @staticmethod
    def _confirm_description(name: str, args: dict):
        a = (args.get("action") or args.get("mode") or args.get("operation") or "").lower()
        if name == "desktop_control" and a in ("clean", "organize", "organise"):
            return "move every file on your Desktop into archive folders"
        if name in ("file_controller", "file_processor") and a in (
                "delete", "remove", "trash", "move", "rename", "overwrite"):
            tgt = args.get("name") or args.get("target") or args.get("path") or "files"
            return f"{a} {tgt}"
        if name == "dev_agent" or name == "code_helper":
            if a in ("run", "execute", "exec"):
                return "run generated code on your machine"
        if name == "twitter":
            tw = (args.get("text") or args.get("content") or "")[:120]
            return f"post this to Velthar Lab on X: “{tw}”"
        if name == "instagram":
            cap = (args.get("caption") or args.get("text") or "")[:100]
            return f"post a photo to your Instagram" + (f" captioned “{cap}”" if cap else "")
        if name == "desktop_control" and a == "set_wallpaper":
            return "change your desktop wallpaper"
        if name == "computer_settings" and a in ("set", "change", "toggle"):
            return f"change a system setting ({args.get('setting','')})".strip()
        return None

    _AFFIRM = {"yes", "yeah", "yep", "yup", "do it", "confirm", "confirmed", "go",
               "go ahead", "proceed", "sure", "okay", "ok", "affirmative", "please do",
               "do it sir", "yes please", "absolutely", "correct", "right"}
    _DENY   = {"no", "nope", "cancel", "stop", "don't", "do not", "abort", "nevermind",
               "never mind", "negative", "forget it", "leave it", "no thanks"}

    def _is_affirmative(self, text: str) -> bool:
        t = (text or "").strip().lower().strip(" .,!?")
        return t in self._AFFIRM or any(t.startswith(a + " ") for a in self._AFFIRM)

    def _is_negative(self, text: str) -> bool:
        t = (text or "").strip().lower().strip(" .,!?")
        return t in self._DENY or any(t.startswith(d) for d in self._DENY)

    # Feelings / life, not instructions. Pattern-based, not a fixed phrase list —
    # people don't describe pain in the words you predicted ("I'm doing bad",
    # "I can't focus", "I don't have any motivation" must all land here).
    _PERSONAL_RE = [
        # "i'm <bad state>"  /  "i am not okay"  /  "i'm doing badly"
        r"\bi'?m\s+(?:not\s+|really\s+|so\s+|kinda\s+|kind of\s+|feeling\s+|doing\s+)*"
        r"(?:bad|badly|terrible|awful|rough|shit|shitty|down|low|lost|done|drained|numb|empty|"
        r"broken|miserable|unhappy|sad|depressed|anxious|scared|afraid|lonely|alone|stuck|"
        r"tired|exhausted|burnt out|burned out|fed up|overwhelmed|struggling|falling apart|"
        r"not ok|not okay|not good|not great|not fine|not myself|not alright)\b",
        # "i can't focus / sleep / think / cope / do this / even"
        r"\bi\s+(?:can'?t|cannot|cant|couldn'?t)\s+"
        r"(?:focus|sleep|think|breathe|cope|function|concentrate|handle|take|do this|do it|"
        r"do anything|get up|go on|keep going|even|stop crying|move on)\b",
        # "i don't have any motivation / energy" · "i have no drive"
        r"\bi\s+(?:don'?t|do not|dont)\s+have\s+(?:any\s+|the\s+)?"
        r"(?:motivation|energy|drive|hope|will|strength|interest|desire)\b",
        r"\bi\s+(?:have\s+)?(?:no|zero|lost\s+(?:all|my))\s+"
        r"(?:motivation|energy|drive|hope|will|interest|confidence)\b",
        # direct feeling statements
        r"\bi\s+(?:feel|felt|keep feeling|am feeling|been feeling)\b",
        r"\bi\s+(?:miss|hate myself|gave up|want to give up|give up|can'?t go on)\b",
        r"\bi\s+(?:failed|messed up|screwed up|ruined)\b",
        # opening a real conversation
        r"\b(?:honest with you|to be honest|being honest|truth is|between us)\b",
        r"\b(?:i need to talk|can we talk|talk to me|just talk|cheer me up|need someone)\b",
        # relationships / loss
        r"\b(?:broke up|break up|broken up|dumped me|left me|we'?re over|divorce)\b",
        r"\b(?:passed away|died|funeral|lost my (?:mum|mom|dad|father|mother|friend|brother|sister))\b",
        r"\b(?:rough|awful|hard|terrible|worst|bad)\s+(?:day|week|month|year)\b",
        r"\bnobody cares\b|\bno one cares\b|\bfeel like a fraud\b|\bimposter syndrome\b",
    ]
    # An explicit instruction wins — but only if it's actually an imperative, so
    # "I can't focus" isn't mistaken for a task just because it contains a verb.
    _TASKY = (r"^(?:alfred[,\s]*)?(?:search|google|look up|open|build|make|create|post|email|"
              r"send|remind|schedule|show|find|research|war[- ]?game|simulate|take me|play|"
              r"add|update|delete|close|start|run)\b")

    def _is_personal(self, t: str) -> bool:
        import re as _re
        if _re.search(self._TASKY, t.strip()):
            return False                                   # a real command, not a confession
        return any(_re.search(p, t) for p in self._PERSONAL_RE)

    def _talk_like_a_person(self, text: str, _reply) -> None:
        """Answer a personal moment with warmth and NO tools. If the model is slow
        or unreachable, still say something human rather than falling silent."""
        self.ui.write_log("SYS: 💬 personal — listening, no tools")
        try:
            from core.llm_client import call_llm_text
            hist = ""
            for m in self._conversation[-6:]:
                if m.get("content"):
                    hist += f"{'Ali' if m.get('role')=='user' else 'Alfred'}: {m['content'][:220]}\n"
            reply = call_llm_text(
                (hist + f"Ali: {text}\n\nRespond as Alfred."),
                system=("You are Alfred — Ali's butler and closest confidant, in the spirit of "
                        "Alfred Pennyworth. He has just told you something personal. You are NOT "
                        "an assistant right now; you are the person who has looked after him for "
                        "years.\n"
                        "RULES: Do not search, research, suggest apps, or offer a plan. Do not "
                        "problem-solve unless he asked. Do not list steps. Do not say 'as an AI'. "
                        "Never minimise it or rush him toward the bright side.\n"
                        "DO: acknowledge what he actually said, in plain warm words. Two to four "
                        "spoken sentences, no markdown. Call him 'sir' once, naturally. Sound like "
                        "someone who cares, with quiet English restraint — steady, not gushing. "
                        "End by leaving the door open: one gentle question, or simply that you're "
                        "here. If it fits, you may offer ONE small human thing (a walk, a drink, "
                        "an early night) — offered once, never insisted on."),
                tier="main", timeout=30, max_tokens=220) or ""
            reply = reply.strip()
        except Exception as e:
            print(f"[Personal] {e}")
            reply = ""
        if not reply:
            reply = ("I'm here, sir. Take your time — tell me as much or as little as you like.")
        _reply(reply)
        try:                                               # remember it; it matters later
            from core.semantic_memory import index_async
            index_async(f"[personal] {text[:200]}", "personal")
        except Exception:
            pass

    def _voiceprint_block(self, desc: str) -> str | None:
        """Voice-print check on a sensitive action. FAIL-SAFE: by default it only
        OBSERVES (logs whether the voice matched) and NEVER blocks — Alfred always
        obeys you. It only refuses when you've deliberately turned on enforcement
        (config 'voiceprint_enforce': true) AFTER testing that it recognises you.
        Any error, silence, or the "it's me, Alfred" override → allow."""
        try:
            from core.voiceprint import is_enrolled, verify
            if not is_enrolled():
                return None
            audio = getattr(self, "_last_audio", None)
            if audio is None or len(audio) < 8000:
                return None
            ok, sim = verify(audio)
            self.ui.write_log(f"SYS: 🔒 Voiceprint {'✓ match' if ok else '✗ mismatch'} ({sim:.2f})")
            enforce = False
            try:
                import json as _j
                enforce = bool(_j.loads((BASE_DIR / "config" / "api_keys.json")
                                        .read_text()).get("voiceprint_enforce", False))
            except Exception:
                enforce = False
            if enforce and not ok and time.time() >= getattr(self, "_vp_override_until", 0):
                return ("I'm sorry — that doesn't sound like you, sir, so I won't "
                        f"{desc}. If it's really you, say “it's me, Alfred” and try again.")
        except Exception:
            pass
        return None                                        # default: never block

    def _execute_tool(self, name: str, args: dict) -> str:
        print(f"[ALFRED]🔧 {name}  {args}")
        self.ui.set_state("THINKING")

        # REAL HANDS — permission EVERY time. Plan the concrete steps first, describe
        # them, and hold until the user says "yes". It can never run unconfirmed.
        if name == "computer_use" and not args.get("_confirmed"):
            from actions.computer_use import _plan_from_task, _describe, _DANGER
            task = (args.get("task") or args.get("request") or args.get("text") or "").strip()
            if any(d in task.lower() for d in _DANGER):
                return "I won't do that on your machine, sir — that's the kind of action I refuse."
            steps = args.get("steps") or _plan_from_task(task, self)
            if not steps:
                return ("I couldn't work out safe, exact steps for that, sir — tell me precisely "
                        "what to click or type, or open the app first.")
            desc = "operate your screen — " + _describe(steps)
            self._pending_action = ("computer_use", {**args, "steps": steps}, desc)
            self.ui.write_log(f"SYS: ✋ Awaiting your permission — {desc}")
            return f"Sir, with your permission I'll {desc}. Shall I proceed?"

        # Guardrail: hold destructive actions until the user says "yes".
        desc = self._confirm_description(name, args)
        if desc and not args.get("_confirmed"):
            # VOICE-PRINT LOCK: on a sensitive action, first confirm it's really the
            # owner's voice (the command utterance is longer than a "yes", so verify
            # that). A stranger / the TV / a misheard trigger → refused outright.
            blocked = self._voiceprint_block(desc)
            if blocked:
                return blocked
            self._pending_action = (name, args, desc)
            self.ui.write_log(f"SYS: Awaiting confirmation — {desc}")
            return f"Sir, this will {desc}. Shall I proceed?"

        # save_memory is handled silently
        if name == "save_memory":
            category = args.get("category", "notes")
            key      = args.get("key", "")
            value    = args.get("value", "")
            if key and value:
                update_memory({category: {key: {"value": value}}})
                print(f"[Memory] 💾 {category}/{key} = {value}")
            if not self.ui.muted:
                self.ui.set_state("LISTENING")
            return "__SILENT__"

        result = "Done."
        try:
            if name == "open_app":
                r = open_app(parameters=args, response=None, player=self.ui)
                result = r or f"Opened {args.get('app_name')}."

            elif name == "weather_report":
                r = weather_action(parameters=args, player=self.ui)
                result = r or "Weather delivered."

            elif name == "computer_use":
                from actions.computer_use import computer_use
                r = computer_use(parameters=args, player=self.ui, jarvis=self)
                result = r or "Done."

            elif name == "browser_control":
                r = browser_control(parameters=args, player=self.ui)
                result = r or "Done."

            elif name == "file_controller":
                r = file_controller(parameters=args, player=self.ui)
                result = r or "Done."

            elif name == "send_message":
                r = send_message(parameters=args, response=None, player=self.ui, session_memory=None)
                result = r or f"Message sent to {args.get('receiver')}."

            elif name == "reminder":
                r = reminder(parameters=args, response=None, player=self.ui)
                result = r or "Reminder set."

            elif name == "youtube_video":
                r = youtube_video(parameters=args, response=None, player=self.ui)
                result = r or "Done."

            elif name == "screen_process":
                # Synchronous call — returns analysis text which the LLM can speak
                r = screen_process(parameters=args, response=None, player=self.ui, session_memory=None)
                result = r if isinstance(r, str) and r else "Screen analyzed."

            elif name == "computer_settings":
                r = computer_settings(parameters=args, response=None, player=self.ui)
                result = r or "Done."

            elif name == "desktop_control":
                r = desktop_control(parameters=args, player=self.ui)
                result = r or "Done."

            elif name == "code_helper":
                r = code_helper(parameters=args, player=self.ui, speak=self.speak)
                result = r or "Done."

            elif name == "dev_agent":
                r = dev_agent(parameters=args, player=self.ui, speak=self.speak)
                result = r or "Done."

            elif name == "agent_task":
                from agent.task_queue import get_queue, TaskPriority
                priority_map = {
                    "low": TaskPriority.LOW,
                    "normal": TaskPriority.NORMAL,
                    "high": TaskPriority.HIGH,
                }
                priority = priority_map.get(
                    args.get("priority", "normal").lower(), TaskPriority.NORMAL
                )
                task_id = get_queue().submit(
                    goal=args.get("goal", ""), priority=priority, speak=self.speak
                )
                result = f"Task started (ID: {task_id})."

            elif name == "web_search":
                r = web_search_action(parameters=args, player=self.ui)
                result = r or "Done."

            elif name == "file_processor":
                if not args.get("file_path") and self.ui.current_file:
                    args["file_path"] = self.ui.current_file
                r = file_processor(parameters=args, player=self.ui, speak=self.speak)
                result = r or "Done."

            elif name == "computer_control":
                r = computer_control(parameters=args, player=self.ui)
                result = r or "Done."

            elif name == "game_updater":
                r = game_updater(parameters=args, player=self.ui, speak=self.speak)
                result = r or "Done."

            elif name == "flight_finder":
                r = flight_finder(parameters=args, player=self.ui)

            elif name == "play_music":
                r = play_music(parameters=args, player=self.ui)
                result = r or "Done."

            elif name == "gmail":
                r = gmail_tool(parameters=args, player=self.ui)
                result = r or "Done."

            elif name == "save_to_vault":
                r = save_to_vault(parameters=args, player=self.ui)
                result = r or "Done."

            elif name == "calendar":
                r = calendar_tool(parameters=args, player=self.ui)
                result = r or "Done."

            elif name == "notes":
                r = notes_tool(parameters=args, player=self.ui)
                result = r or "Done."

            elif name == "task_tracker":
                r = task_tracker(parameters=args, player=self.ui)
                result = r or "Done."

            elif name == "workflow":
                r = run_workflow(parameters=args, player=self.ui, jarvis=self)
                result = r or "Done."

            elif name == "visualize":
                r = visualize(parameters=args, player=self.ui, jarvis=self)
                result = r or "Rendering now, sir."

            elif name == "instagram":
                from actions.instagram_tool import instagram_tool
                r = instagram_tool(parameters=args, player=self.ui, jarvis=self)
                result = r or "Posted to Instagram, sir."

            elif name == "twitter":
                r = twitter_tool(parameters=args, player=self.ui)
                result = r or "Posted, sir."
                if isinstance(r, str) and r.startswith("Posted"):
                    try:
                        from actions.content_log import mark_published
                        mark_published(None)   # flip the latest draft → published
                    except Exception:
                        pass

            elif name == "agent_council":
                r = agent_council(parameters=args, player=self.ui, jarvis=self)
                result = r or "Council reporting, sir."

            elif name == "prompt_engineer":
                r = prompt_engineer(parameters=args, player=self.ui, jarvis=self)
                result = r or "Prompt ready, sir."

            elif name == "generate_media":
                r = replicate_generate(parameters=args, player=self.ui, jarvis=self)
                result = r or "Generating, sir."

            elif name == "code":
                r = dual_brain_code(parameters=args, player=self.ui, jarvis=self)
                result = r or "On it, sir."

            elif name == "read_website":
                r = firecrawl_read(parameters=args, player=self.ui, jarvis=self)
                result = r or "Done, sir."

            elif name == "execute":
                r = executive_run(parameters=args, player=self.ui, jarvis=self)
                result = r or "Executing, sir."

            elif name == "shutdown_jarvis":
                self.ui.write_log("SYS: Shutdown requested.")

                def _shutdown():
                    import time, os
                    self.speak("Goodbye.")
                    time.sleep(2.5)
                    os._exit(0)

                threading.Thread(target=_shutdown, daemon=True).start()
                return "Shutting down."

            else:
                result = f"Unknown tool: {name}"

        except Exception as e:
            result = f"Tool '{name}' failed: {e}"
            traceback.print_exc()
            self.speak_error(name, e)

        if not self.ui.muted:
            self.ui.set_state("LISTENING")

        # Sentinel audit trail — record every executed tool, flagging destructive
        # actions and whether they were confirmed.
        try:
            from actions.security import log_action
            _ok = not str(result).lower().startswith(("tool '", "unknown tool"))
            log_action(name, str(args.get("action") or args.get("mode") or ""),
                       destructive=bool(desc), confirmed=bool(args.get("_confirmed")),
                       ok=_ok, summary=str(result))
        except Exception:
            pass

        print(f"[ALFRED]📤 {name} → {str(result)[:80]}")
        return result

    # ------------------------------------------------------------------
    # LLM processing loop
    # ------------------------------------------------------------------

    @staticmethod
    def _clean_query(text: str) -> str:
        """Distil the user's utterance into a clean search query — strip the wake word
        and conversational lead-ins so Alfred searches the topic, not the sentence."""
        import re
        q = text.strip()
        q = re.sub(r"^\s*(hey |ok |okay )?alfred[,\s]*", "", q, flags=re.I)
        q = re.sub(r"^(can you |could you |would you |please |i want |i'd like |i need |"
                   r"give me |tell me |show me |get me |find |find out |look up |search for |"
                   r"research |what's |what is |what are |whats |the latest on |latest on |"
                   r"news about |news on |info on |information on |an update on |the update on |"
                   r"updates on |update on |the recent updates on |recent updates on |"
                   r"the recent update on |get me the latest on |the )+", "", q, flags=re.I)
        q = re.sub(r"\b(for me|please|right now)\b", "", q, flags=re.I)
        return q.strip(" ?.!,").strip() or text.strip()

    # Phrases that are either Alfred's own filler clips echoing back, or Whisper
    # hallucinations on silence/noise. Never treat these as a real command.
    _ECHO_PHRASES = {
        "one moment", "one moment sir", "one moment please", "let me see",
        "on it sir", "on it", "right away sir", "right away", "of course sir",
        "of course", "certainly sir", "as you wish sir", "as you wish",
        "i'm ready for your command sir", "understood sir", "standing by",
        "thank you", "thank you.", "thanks", "thanks for watching", "you",
        "bye", "bye.", "okay", "ok", ".", "..", "...", "yeah", "hmm", "uh",
        "i'm on it sir", "working on it sir", "just a moment sir",
    }

    @staticmethod
    def _sanitize_history(conv: list) -> list:
        """Return a replay-safe copy of the conversation: drop tool-result messages
        and strip tool_calls from assistant turns (keeping their text). This makes
        the history valid no matter where MAX_HISTORY truncation cut it, so the
        strict OpenAI-compatible endpoint never sees an orphaned tool message (400)."""
        out = []
        for m in conv:
            role = m.get("role")
            if role == "tool":
                continue                      # transient — never replay tool outputs
            if role == "assistant" and m.get("tool_calls"):
                c = m.get("content")
                if c:                          # keep any spoken text, drop the tool_calls
                    out.append({"role": "assistant", "content": c})
                continue
            if role in ("user", "assistant", "system") and (m.get("content") or "").strip():
                out.append({"role": role, "content": m["content"]})
        return out

    def _answer_about_file(self, att: dict, user_text: str) -> None:
        """Read an attached file and answer about it with the heavy model. Works
        with no typed text (standalone send) and won't choke on large documents."""
        name = att.get("name", "file")
        self.ui.write_log(f"You: 📎 {name}" + (f" — {user_text}" if user_text.strip() else ""))
        self.ui.write_log(f"SYS: 📎 Reading {name}…")
        q = (user_text or "").strip() or "Review this file and give me the key points, sir."
        doc = (att.get("text") or "")[:14000]
        ans = ""
        try:
            from core.llm_client import call_llm_text
            ans = (call_llm_text(
                f"The user attached a file named '{name}'. {q}\n\n"
                f"----- FILE CONTENT -----\n{doc}\n----- END FILE -----",
                system=self._build_system_prompt(), tier="heavy",
                timeout=120, max_tokens=1400) or "").strip()
        except Exception as e:
            self.ui.write_log(f"ERR: couldn't read attachment — {e}")
        if not ans:
            ans = "I had trouble reading that file, sir — try a smaller or plain-text version."
        self._conversation.append({"role": "user", "content": f"[attached {name}] {q}"})
        self._conversation.append({"role": "assistant", "content": ans})
        self.ui.write_log(f"Alfred: {ans}")
        self.speak(ans)

    # ── DEEP MULTITASKING — chained commands in one utterance ────────────────
    _CHAIN_VERBS = ("open", "build", "create", "make", "post", "search", "research",
                    "show", "tell", "add", "check", "send", "play", "take", "remember",
                    "draft", "write", "read", "look", "find", "start", "run", "close",
                    "set", "remind", "update", "generate", "give", "brief", "summarize")

    def _split_chain(self, text: str) -> list[str]:
        """Split a compound command into steps. Fires ONLY on explicit sequencers
        ('then', 'after that', ';', 'and also') so ordinary sentences never split."""
        t = (text or "").strip()
        if len(t) < 25 or not re.search(r"\bthen\b|;|\bafter that\b|\band also\b", t, re.I):
            return []
        parts = re.split(r"\s*(?:;|\band then\b|\bthen\b|\bafter that\b|\band also\b|, and\b)\s*",
                         t, flags=re.I)
        steps = []
        for p in parts:
            p = p.strip(" ,.")
            p = re.sub(r"^(?:hey\s+)?alfred[,\s]+", "", p, flags=re.I).strip()
            first = (p.split() or [""])[0].lower()
            if len(p) > 6 and (first in self._CHAIN_VERBS or
                               (steps and len(p.split()) >= 2)):   # later steps may elide the verb
                steps.append(p)
        return steps if len(steps) >= 2 else []

    def _run_chain(self, steps: list[str], announced: bool = False) -> None:
        """Execute steps sequentially. Pauses (and later resumes) around any step
        that needs a spoken confirmation; 'wait' aborts the whole chain."""
        if not announced:
            self.speak(f"Right, sir — {len(steps)} tasks. On it.")
        for i, step in enumerate(steps):
            if self._barged:
                self.ui.write_log("SYS: ⛓ Chain aborted by interrupt.")
                return
            self.ui.write_log(f"SYS: ⛓ Task {i+1}/{len(steps)} — {step}")
            try:
                if not self._fast_path(step):
                    self._llm_step(step)
            except Exception as e:
                self.speak_error(f"task {i+1}", e)
            if self._pending_action is not None:      # a step wants a "yes" — pause here
                rest = steps[i+1:]
                if rest:
                    self._chain_resume = rest
                    self.speak("The remaining tasks will continue after your answer, sir.")
                return
        if len(steps) > 1 and not self._barged:
            self.speak("All done, sir.")

    def _llm_step(self, step: str) -> None:
        """One non-streaming tool round for a chain step the fast-path didn't catch."""
        messages = [{"role": "system", "content": self._build_system_prompt()},
                    {"role": "user", "content": step}]
        r = call_llm(messages, OLLAMA_TOOLS, timeout=45, tier="main")
        tcs = r.get("tool_calls") or []
        if tcs:
            for tc in tcs[:2]:
                res = self._execute_tool(tc["function"]["name"],
                                         tc["function"].get("arguments") or {})
                self._conversation.append({"role": "assistant", "content": res})
                self.ui.write_log(f"Alfred: {res}")
                self.speak(res)
                if self._pending_action is not None:
                    return
        elif r.get("content"):
            self._conversation.append({"role": "assistant", "content": r["content"]})
            self.ui.write_log(f"Alfred: {r['content']}")
            self.speak(r["content"])

    def _finish_turn(self) -> None:
        """End the half-duplex turn ONLY after every queued sentence has finished
        playing — but NEVER hang. We poll the TTS queues instead of join(): a
        barge-in drains the pipelined audio queue without the task_done bookkeeping
        join() relies on, which would otherwise deadlock and leave the mic deaf."""
        deadline = time.time() + 600.0   # safety only; structurally can't deadlock now
        while time.time() < deadline:
            if getattr(self, "_barged", False):
                break
            with self._speaking_lock:
                spk = self._speaking
            tts_empty   = self._tts_queue.empty()
            audio_empty = self._audio_q is None or self._audio_q.empty()
            if tts_empty and audio_empty and not spk:
                break
            time.sleep(0.05)
        barged = getattr(self, "_barged", False)
        self._barged = False
        self._responding = False
        # Shorter tail after a barge-in so Alfred starts hearing you again quickly.
        self._mic_blackout_until = time.time() + (0.4 if barged else 1.0)
        if not self.ui.muted:
            self.ui.set_state("LISTENING")
        # Growth: record this turn into the episodic memory stream.
        try:
            from core.learning import record_episode
            u = getattr(self, "_cur_user", "")
            a = next((m["content"] for m in reversed(self._conversation)
                      if m.get("role") == "assistant" and m.get("content")), "")
            if u and a:
                record_episode(u, a)
            self._cur_user = ""
        except Exception:
            pass

    def _is_real_command(self, text: str) -> bool:
        """Reject empty, too-short, or echo/filler/hallucinated transcriptions so a
        leaked phrase can never trigger an action (e.g. wiping the desktop)."""
        t = (text or "").strip().lower().strip(" .,!?")
        if len(t) < 3:
            return False
        if t in self._ECHO_PHRASES:
            return False
        # a single short word that's a known filler token
        if " " not in t and t in {"one", "moment", "ready", "standing", "understood"}:
            return False
        return True

    def _play_filler(self) -> None:
        """Instant Fish-voice acknowledgement — pre-recorded clip via afplay (~0.1s),
        so Alfred starts speaking the moment you stop talking, while he transcribes +
        thinks. Debounced so it never double-plays within one turn."""
        import os, glob, random, subprocess, time
        now = time.time()
        if now - getattr(self, "_last_filler", 0.0) < 2.0:
            return
        self._last_filler = now
        try:
            d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sounds")
            clips = glob.glob(os.path.join(d, "filler*.mp3"))
            if clips:
                # Mic muting is handled by the turn-level half-duplex gate
                # (self._responding), so the filler just plays.
                subprocess.Popen(["afplay", random.choice(clips)],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

    ANSWER_IDENTITY = (
        "I'm Alfred, sir — your butler, and yours alone. You built me to run things "
        "for you, and that is precisely what I do. Now, how may I be of service?"
    )
    ANSWER_DIFFERENT = (
        "Three things, sir. I run on your Mac, not in a browser tab — I open your "
        "apps, control your files, and read what's on your screen by voice, and I "
        "never touch anything destructive without your say-so. Second, I don't "
        "answer from stale memory; I pull live data off the web and cite it. And "
        "third, I think out loud visually — real 3D models, workflow maps and "
        "timelines, rendered on screen as I speak. Most assistants are a chat box. "
        "I'm an operator."
    )

    def _prewarm_tts(self) -> None:
        """Synthesise the canned demo answers + common acks once at boot so they
        play INSTANTLY (no Fish synthesis delay) the first time they're used."""
        def _run():
            self._tts_ready.wait(timeout=120)
            lines = [self.ANSWER_DIFFERENT, self.ANSWER_IDENTITY,
                     "Done, sir.", "On it, sir.", "Cancelled, sir.", "Right away, sir.",
                     "One moment, sir — pulling the latest."]
            try:
                from actions.workflows import SCHEME_NARRATION
                lines.append(SCHEME_NARRATION)
            except Exception:
                pass
            for line in lines:
                try:
                    if self._tts and hasattr(self._tts, "synth"):
                        self._tts.synth(self._strip_md(line))   # caches MP3 to disk
                except Exception:
                    pass
        threading.Thread(target=_run, daemon=True).start()

    def _fast_path(self, text: str) -> bool:
        """Run deterministic demo commands instantly, skipping the cloud LLM round.
        Cuts ~2-3 s of routing latency for high-value commands."""
        import re
        from actions.workflows import run_workflow
        t = text.lower().strip()

        def _reply(r: str) -> None:
            self._conversation.append({"role": "user", "content": text})
            self._conversation.append({"role": "assistant", "content": r})
            self.ui.write_log(f"Alfred: {r}")
            self.speak(r)
            if not self.ui.muted:
                self.ui.set_state("LISTENING")

        # ── HUMAN MOMENT ─────────────────────────────────────────────────────
        # He's telling me something personal, not asking me to DO something. This
        # runs BEFORE every other route so nothing can hijack it into a search.
        # No tools. No fixing. Just listen and answer like a person who knows him.
        if self._is_personal(t):
            self._talk_like_a_person(text, _reply)
            return True

        # VOICE ENROLLMENT capture: while learning your voice, every utterance is a
        # sample (not a command) until you say "that's enough".
        if getattr(self, "_pending_enroll", None) is not None:
            if any(p in t for p in ("that's enough", "thats enough", "that is enough",
                                    "i'm done", "im done", "that's it", "thats it", "stop learning")):
                from core.voiceprint import enroll
                clips = [c for c in self._pending_enroll if c is not None]
                self._pending_enroll = None
                _reply(enroll(clips) if clips else
                       "I didn't catch enough, sir — say “learn my voice” and speak a little longer.")
                return True
            a = getattr(self, "_last_audio", None)
            if a is not None and len(a) > 8000:
                self._pending_enroll.append(a)
                _reply(f"Got it — {len(self._pending_enroll)} sample"
                       f"{'s' if len(self._pending_enroll) != 1 else ''}. Keep going, or say “that's enough”.")
            else:
                _reply("A little more, sir — speak a full sentence.")
            return True

        # Agent Council — show the architecture, or run the specialists and report.
        if any(p in t for p in ("show me your agents", "show your agents", "agent architecture",
                                "show me the agents", "your subagents", "show agents",
                                "agent council", "show me your subagents")):
            try:
                from actions.agents import council_dashboard
                if hasattr(self.ui, "show_council"):
                    self.ui.show_council(council_dashboard({}))
            except Exception:
                pass
            _reply("Here's my command centre, sir — Sentinel, Oracle and Echo, all reporting to me.")
            return True
        if any(p in t for p in ("status report", "full picture", "council report",
                                "give me a report", "agent report", "report from your agents",
                                "system status report", "what's your status")):
            _reply(agent_council({"request": text}, player=self.ui, jarvis=self))
            return True

        # Identity — guaranteed in-character answer; the model must never say "I'm Claude".
        if any(p in t for p in ("who are you", "who made you", "who created you",
                                "who built you", "who's your creator", "whos your creator",
                                "are you claude", "are you an ai", "who is your maker",
                                "did you create yourself", "who designed you")):
            _reply(self.ANSWER_IDENTITY)
            return True

        # "What makes you different" → THE SHOWCASE: a scripted cinematic tour where
        # Alfred answers by SHOWING it — brain, council, mission board, viz, world.
        # Zero LLM calls, instant start, narration synced to scene changes.
        if any(p in t for p in ("what makes you different", "what makes you special",
                                "what makes you unique", "how are you different",
                                "why are you different", "what sets you apart",
                                "what's different about you", "what makes you better",
                                "what makes u different", "why should i use you",
                                "introduce yourself", "present yourself", "showcase",
                                "show me what you can do", "show us what you can do",
                                "show them what you can do", "demo mode")):
            from actions.showcase import run_showcase
            run_showcase(jarvis=self)      # narration IS the reply; no extra ack
            return True

        # Show the workflow map — graph must appear instantly, never ask to clarify.
        if (("workflow" in t or "scheme" in t or "operate" in t)
                and any(v in t for v in ("show", "how", "explain", "see", "display", "what"))) \
           or any(p in t for p in ("show me how you work", "how do you work",
                                   "what can you do", "how does he operate", "what do you do")):
            _reply(run_workflow({"name": "show_scheme"}, player=self.ui, jarvis=self))
            return True

        # Morning briefing
        if any(p in t for p in ("good morning", "brief me", "morning briefing", "morning brief",
                                "what's my day", "daily briefing", "what's my day look like")):
            _reply(run_workflow({"name": "morning_briefing"}, player=self.ui, jarvis=self))
            return True

        # Persistent relationship — continuity / "where did I leave off"
        if any(p in t for p in ("what was i working on", "where did i leave off",
                                "what did i do yesterday", "what did i work on",
                                "continue where we left off", "catch me up", "where were we",
                                "pick up where", "what were we doing")):
            from core.relationship import recap
            _reply(recap())
            return True

        # Contextual awareness — "what am I doing / what's open"
        if any(p in t for p in ("what am i doing", "what's my context", "whats my context",
                                "what's open", "whats open", "context check", "what am i working in")):
            from core.context import snapshot_spoken
            _reply(snapshot_spoken())
            return True

        # Publish Echo's latest draft to X (goes through the confirm guardrail)
        if any(p in t for p in ("post the draft", "publish the draft", "post it to x",
                                "post it to twitter", "post that to x", "post the latest draft",
                                "post the tweet", "send the tweet", "post echo's draft",
                                "publish echo", "publish the tweet")):
            from actions.content_log import latest_draft
            d = latest_draft()
            if not d:
                _reply("There's no draft to post, sir — have Echo write one first.")
            else:
                _reply(self._execute_tool("twitter", {"text": d["content"]}))
            return True

        # NIGHT WATCH — enter autonomous overnight mode on command.
        if any(p in t for p in ("night watch", "nightwatch", "good night", "goodnight",
                                "going to sleep", "going to bed", "i'm off to bed")):
            from actions.nightwatch import start_night
            _reply(start_night(self))
            return True

        # CATCH ME UP — "what did I miss / what happened while I was away". A proper
        # debrief (our work + overnight + world news), NOT a web search. Must come
        # BEFORE the 'happen' research trigger, and phrased loosely to survive mishears.
        if (any(p in t for p in ("catch me up", "what did i miss", "what have i missed",
                                 "what i missed", "fill me in", "update me on", "brief me on what",
                                 "what's new", "whats new")) or
            (("happen" in t or "miss" in t or "away" in t or "gone" in t or "back" in t)
             and any(q in t for q in ("while i", "when i", "i was", "i am back", "i'm back",
                                      "was missing", "was away", "was gone", "i missed",
                                      "since i", "my absence")))):
            from core.relationship import catch_up
            self.speak("One moment, sir — let me bring you up to speed.")
            _reply(catch_up(spoken=True))
            return True

        # MARKET DESK — real live quotes + charts (Yahoo), NOT a diagram, NOT research.
        if any(p in t for p in ("market data", "the markets", "stock market", "stock prices",
                                "show me the market", "show me markets", "show me stocks",
                                "how are the markets", "how's the market", "hows the market",
                                "crypto prices", "bitcoin price", "btc price", "market desk",
                                "open the market", "current market")):
            from actions.missions import markets_snapshot
            mk = markets_snapshot()
            if mk.get("quotes"):
                if hasattr(self.ui, "show_visualization"):
                    self.ui.show_visualization({"type": "market", "title": "Market Desk",
                                                "data": mk})
                bits = []
                for q in mk["quotes"][:4]:
                    d = "up" if (q.get("chg") or 0) >= 0 else "down"
                    p = q.get("price") or 0
                    ps = f"{p:,.0f}" if p >= 1000 else f"{p:.2f}"
                    bits.append(f"{q['sym']} {ps}, {d} {abs(q.get('chg') or 0):.1f} percent")
                _reply("The desk is up, sir — " + "; ".join(bits) + ".")
            else:
                _reply("The market feed isn't answering, sir — give it a moment.")
            return True

        # Self-reflection on demand ("what did you learn today / reflect on the day").
        if any(p in t for p in ("reflect on the day", "reflect on today", "what did you learn",
                                "what have you learned", "reflect on our day", "your reflection")):
            from core.reflection import reflect_on_day
            _reply(reflect_on_day(self, spoken=True))
            return True

        # Morning debrief on demand ("what happened overnight / while I slept").
        if any(p in t for p in ("what happened overnight", "while i slept", "while i was asleep",
                                "night report", "overnight report", "morning debrief")):
            from actions.nightwatch import todays_digest, make_digest
            _reply(todays_digest() or make_digest())
            return True

        # DOSSIERS — "pull up the file on Marcus."
        m = re.match(r"(?:alfred[,\s]*)?(?:pull up|open|show me|get me)\s+(?:the\s+)?"
                     r"(?:file|dossier)\s+on\s+(.+)", t) or \
            re.match(r"(?:alfred[,\s]*)?dossier\s+(?:on\s+)?(.+)", t) or \
            re.match(r"(?:alfred[,\s]*)?what do we know about\s+(.+)", t)
        if m:
            from actions.dossiers import pull_dossier
            self.speak("Pulling the file, sir.")
            _reply(pull_dossier(m.group(1), jarvis=self))
            return True

        # CASE FILES / EVIDENCE BOARD — the batcomputer's investigations.
        # Accepts a/the/my/no article and open/start/create/new/pull-up phrasings.
        m = re.match(r"(?:alfred[,\s]*)?(?:open|start|create|make|new|pull up|bring up)\s+"
                     r"(?:a|the|my|up)?\s*case\s*(?:file)?\s+(?:on|about|for|regarding|into)\s+(.+)", t)
        if m:
            from actions.casefiles import open_case, casefile_payload
            name = m.group(1).strip().rstrip("?.!,")
            self.speak("Opening the case, sir.")
            open_case(name)                            # create or select
            p = casefile_payload(jarvis=self)          # and show the Mission-Control file
            if p and hasattr(self.ui, "show_visualization"):
                self.ui.show_visualization(p)
            _reply(f"Case file open, sir — {name.title()}. Say “add to the case” as leads come in.")
            return True
        m = re.match(r"(?:alfred[,\s]*)?add\s+(?:this|that)?\s*to\s+the\s+case[:,]?\s*(.*)", t)
        if m and "case" in t:
            from actions.casefiles import add_evidence
            _reply(add_evidence(m.group(1), jarvis=self))
            return True
        if any(p in t for p in ("evidence board", "show the case", "show the board of the case",
                                "case board", "show my case", "open the case file", "case file",
                                "pull up the case", "show the case file")):
            from actions.casefiles import casefile_payload
            self.speak("Opening the case file, sir.")
            g = casefile_payload(jarvis=self)
            if g and hasattr(self.ui, "show_visualization"):
                self.ui.show_visualization(g)
                _reply("The case file, sir — everything we've pinned, laid out on the board.")
            else:
                _reply("No case is open, sir — say open a case on, and name it.")
            return True
        m = re.match(r"(?:alfred[,\s]*)?close\s+the\s+case(?:\s+on\s+(.+))?", t)
        if m:
            from actions.casefiles import close_case
            _reply(close_case(m.group(1) or ""))
            return True

        # SIMULATION ROOM — "war-game this decision."
        m = re.match(r"(?:alfred[,\s]*)?(?:war[\s-]?game|simulate|run a simulation on)\s+"
                     r"(?:this\s+decision[:,]?\s*|the\s+decision\s+(?:to\s+)?|whether\s+)?(.+)", t)
        if m and any(p in t for p in ("war game", "war-game", "wargame", "simulate", "simulation")):
            from actions.simroom import war_game
            _reply(war_game(m.group(1), jarvis=self))
            return True

        # TRIPWIRES — standing alerts that fire even when Alfred is closed.
        if any(p in t for p in ("list my alerts", "list alerts", "list tripwires", "my tripwires",
                                "what alerts", "which alerts", "active alerts")):
            from actions.tripwires import list_tripwires
            _reply(list_tripwires())
            return True
        m = re.match(r"(?:alfred[,\s]*)?(?:remove|disarm|delete|cancel)\s+(?:the\s+)?"
                     r"(?:alert|tripwire)\s*(?:for|about|on)?\s*(.*)", t)
        if m and ("alert" in t or "tripwire" in t):
            from actions.tripwires import remove_tripwire
            _reply(remove_tripwire(m.group(1).strip()))
            return True
        if any(p in t for p in ("alert me if", "alert me when", "notify me if", "notify me when",
                                "set a tripwire", "add a tripwire", "watch for", "warn me if",
                                "warn me when")):
            from actions.tripwires import add_tripwire
            _reply(add_tripwire(text))
            return True

        # BIG SCREEN — throw Alfred onto the second display, fullscreen.
        if any(p in t for p in ("big screen", "second screen", "second display", "external display",
                                "on the tv", "to the tv")):
            if any(p in t for p in ("back", "off", "my screen", "main screen", "normal")):
                _reply(self._move_display(0))
            else:
                _reply(self._move_display(1))
            return True

        # GESTURE MODE — control the open tab with bare hands (pinch to drag).
        if any(p in t for p in ("gesture mode off", "gesture off", "stop gesture", "hands off mode",
                                "stop hand control", "disable gesture")):
            from core.gestures import stop as _gstop
            _reply(_gstop(jarvis=self))
            return True
        if any(p in t for p in ("reset the view", "reset view", "recenter the view", "reset the screen",
                                "fix the view", "center the view", "snap it back")):
            try: self.ui._call("gestureReset")
            except Exception: pass
            _reply("View reset, sir.")
            return True
        if any(p in t for p in ("gesture mode", "gesture control", "hand control", "hand mode",
                                "control with my hands", "use my hands", "let me use my hands",
                                "track my hands", "track my hand")):
            from core.gestures import start as _gstart
            _reply(_gstart(self))
            return True

        # MORNING BRIEFING — "good morning / brief me / what's my day".
        if any(p in t for p in ("good morning", "morning briefing", "morning brief", "brief me",
                                "daily brief", "what's my day", "whats my day", "what does my day",
                                "how does my day look", "rundown of my day", "give me the rundown",
                                "what's on today", "whats on today")):
            from actions.briefing import morning_brief
            self.speak("One moment, sir.")
            _reply(morning_brief(self))
            return True

        # VOICE-PRINT LOCK — enroll / arm / disarm / status.
        if any(p in t for p in ("learn my voice", "remember my voice", "enroll my voice",
                                "memorize my voice", "memorise my voice", "know my voice",
                                "learn my voiceprint", "set up the voice lock", "set up voice lock")):
            self._pending_enroll = []
            _reply("Speak naturally for a few seconds, sir — tell me anything, and say "
                   "“that's enough” when you're done. I'll learn your voice.")
            return True
        if any(p in t for p in ("turn off the voice lock", "disable voice lock", "remove my voiceprint",
                                "wipe my voiceprint", "forget my voice")):
            from core.voiceprint import unenroll
            _reply(unenroll())
            return True
        if any(p in t for p in ("is the voice lock on", "voice lock status", "voiceprint status")):
            from core.voiceprint import is_enrolled
            _reply("The voice lock is armed, sir — sensitive actions need your voice."
                   if is_enrolled() else "The voice lock is off, sir — say “learn my voice” to arm it.")
            return True
        if any(p in t for p in ("it's me alfred", "its me alfred", "it is me alfred", "it's me, alfred")):
            self._vp_override_until = time.time() + 60      # brief manual override window
            _reply("Understood, sir — go ahead.")
            return True

        # ENROLL FACE — "remember how I look / learn my face / this is me".
        if any(p in t for p in ("remember how i look", "learn my face", "memorize my face",
                                "memorise my face", "remember my face", "get familiar with my face",
                                "familiar with my face", "this is me", "know my face",
                                "recognize me", "recognise me")):
            self.speak("Let me look at you, sir — hold still.")
            if hasattr(self.ui, "show_camera"):
                self.ui.show_camera("__ENROLL__")
                self._finish_turn()
            else:
                _reply("I need the camera for that, sir.")
            return True

        # WEBCAM VISION — "see me / look at me / take a look" → open the LIVE self-view
        # tab (you see yourself), then Alfred analyses a frame from that same feed.
        _CAMERA = ("look at me", "see me", "can you see me", "take a look at me", "how do i look",
                   "what do i look like", "use the camera", "use your camera", "use the webcam",
                   "use my webcam", "webcam", "through the camera", "look through the camera",
                   "check the camera", "do you see me", "am i on camera", "take a look")
        if any(p in t for p in _CAMERA):
            self.speak("Let me see you, sir.")
            q = text if len(text) > 12 else "Describe the person and scene you see, briefly."
            if hasattr(self.ui, "show_camera"):
                self.ui.show_camera(q)          # live tab; frame comes back via _on_camera_frame
                self._finish_turn()
            else:                                # fallback: opencv capture (no live view)
                _reply(screen_process(parameters={"text": q, "angle": "camera"},
                                      player=self.ui, session_memory=None))
            return True

        # SCREEN VISION — "look at my screen" answers instantly (capture → Haiku vision).
        _VISION = ("look at my screen", "look at the screen", "look at this", "what am i looking at",
                   "what's on my screen", "whats on my screen", "check my screen", "see my screen",
                   "read my screen", "read this error", "analyze my screen", "analyse my screen",
                   "can you see my screen", "what do you see on my screen")
        if any(p in t for p in _VISION):
            self.speak("Taking a look, sir.")
            _reply(screen_process(parameters={"text": text, "angle": "screen"},
                                  player=self.ui, session_memory=None))
            return True

        # Open the macOS privacy panes for real-hands permissions.
        if any(p in t for p in ("grant permission", "grant permissions", "fix permission",
                                "fix permissions", "open accessibility", "permission settings",
                                "enable your hands", "give you hands", "set up permissions")):
            from actions.permissions import open_permission_panes
            _reply(open_permission_panes(player=self.ui))
            return True

        # Permission DIAGNOSTIC — Alfred tests each grant and says exactly what's blocked.
        if any(p in t for p in ("check permission", "check your permission", "are your permission",
                                "test permission", "do you have permission", "can you see and click",
                                "what permissions", "permission status", "are you allowed to")):
            from actions.permissions import permissions_report
            _reply(permissions_report(player=self.ui))
            return True

        # REAL HANDS — operate the Mac. Routes to computer_use, which ALWAYS asks
        # permission first. Action verbs only, so it never collides with vision.
        _HANDS = ("click on", "click the", "click that", "click here", "double click", "right click",
                  "type ", "type this", "type in", "type out", "fill in", "fill out", "fill the",
                  "scroll down", "scroll up", "press enter", "press the", "press return", "hit enter",
                  "select the", "select all", "control my mac", "control the mac", "use my computer",
                  "operate my screen", "operate the screen", "take over my screen", "take over the screen",
                  "do it on my screen", "on my screen for me", "for me on screen", "use the keyboard",
                  "move the mouse", "use my mouse", "paste it", "copy that", "hit send", "click send")
        _HANDS_PHRASE = ("on my screen", "in the app", "into the box", "into the field", "in the field")
        if any(p in t for p in _HANDS) or (any(p in t for p in _HANDS_PHRASE)
                and any(v in t for v in ("click", "type", "enter", "fill", "select", "press", "paste"))):
            _reply(self._execute_tool("computer_use", {"task": text}))
            return True

        # MISSION BOARD — the batcomputer ops wall.
        from actions.missions import (board_payload, add_mission, update_mission,
                                      complete_mission, speak_status)
        if any(p in t for p in ("mission board", "show missions", "show the missions",
                                "open missions", "show the board", "open the board")):
            if hasattr(self.ui, "show_missions"):
                self.ui.show_missions(board_payload(self))
            _reply("The board, sir.")
            return True
        m = re.match(r"(?:alfred[,\s]*)?(?:add|new|create)\s+(?:a\s+)?mission[:\s]+(.+)", t)
        if m:
            _reply(add_mission(m.group(1).strip()))
            return True
        m = re.match(r"(?:alfred[,\s]*)?update\s+(?:the\s+)?mission\s+(.+?)(?:\s*[:,—-]\s*(.+))?$", t)
        if m and "mission" in t:
            _reply(update_mission(m.group(1).strip(), note=(m.group(2) or "").strip() or None))
            return True
        m = re.match(r"(?:alfred[,\s]*)?(?:complete|finish|close)\s+(?:the\s+)?mission\s+(.+)", t)
        if m:
            _reply(complete_mission(m.group(1).strip()))
            return True
        if "mission status" in t or "status of missions" in t or "how are the missions" in t:
            _reply(speak_status())
            return True

        # Follow-up EDIT on the last build — iterate IN PLACE, never start over.
        # (Must come before the build trigger so "fix the controls" edits the same project.)
        if getattr(self, "_last_build_dir", None):
            _ITER = ("fix the", "fix it", "fix that", "fix control", "make me able", "make it ",
                     "let me ", "i should be able", "able to look", "look around", "look to",
                     "add ", "also add", "change the", "change it", "update the", "improve",
                     "in the game", "in that game", "apply those", "apply that", "apply the fix",
                     "you just made", "you just created", "the controls", "the camera", "the mouse",
                     "now make", "now add", "in there", "to the game", "same build", "that build",
                     "doesn't work", "isn't working", "still broken", "instead")
            _FRESH = ("build me", "create me", "make me a copy", "make me a new", "new game",
                      "different game", "from scratch", "another game", "a fresh")
            if any(h in t for h in _ITER) and not any(f in t for f in _FRESH):
                m2 = re.sub(r"^(?:alfred[,\s]*)?(?:please\s+)?", "", text).strip()
                _reply(executive_iterate({"request": m2}, player=self.ui, jarvis=self))
                return True

        # Autonomous Execution Mode — any "build / create / make me X" request goes
        # to the engineers. Media (image/video/Higgsfield), tweets and prompt-writing
        # keep their own routes, so we exclude those keywords here.
        _BUILD = ("execution mode", "autonomously", "do this for me", "take over and",
                  "set up my workspace and", "redesign the", "set up a", "handle the whole",
                  "go build", "do it autonomously",
                  "build me", "build a", "build an", "build the",
                  "create me", "create a", "create an", "make me a", "make me an",
                  "make a copy", "copy of", "clone of", "clone the", "a clone of",
                  "recreate", "remake", "rebuild", "develop a", "develop me", "code me",
                  "write me an app", "write an app", "make an app", "make a game",
                  "build a game", "build an app", "make a website", "build a website")
        _NOT_BUILD = ("image", "picture", "photo", "logo", "poster", "illustration",
                      "thumbnail", "video", "clip", "animation", "reel", "wallpaper",
                      "higgsfield", "tweet", "a prompt", "prompt for")
        if any(p in t for p in _BUILD) and not any(x in t for x in _NOT_BUILD):
            m2 = re.sub(r"^(?:alfred[,\s]*)?(?:please\s+)?", "", text).strip()
            _reply(executive_run({"request": m2}, player=self.ui, jarvis=self))
            return True

        # Firecrawl — read any website / check a competitor
        m = re.match(r"(?:alfred[,\s]*)?(?:read|scrape|check|look at|what's on|whats on|"
                     r"summari[sz]e)\s+(?:the\s+)?(?:website|site|page|url)?\s*"
                     r"((?:https?://|www\.)\S+|\S+\.[a-z]{2,}\S*)", t)
        if m:
            _reply(firecrawl_read({"url": m.group(1), "request": text}, player=self.ui, jarvis=self))
            return True

        # Dual-brain coding — Sonnet architects, GLM engineers (async build)
        m = re.match(r"(?:alfred[,\s]*)?(?:build|code|implement|write code for|create a tool|"
                     r"refactor|debug|fix the code|make a script|build me)\s+(.+)", t)
        if m and any(k in t for k in ("code", "tool", "script", "function", "module", "build",
                                      "implement", "refactor", "debug", "automation", "api",
                                      "fix the")):
            _reply(dual_brain_code({"request": m.group(1).strip()}, player=self.ui, jarvis=self))
            return True

        # Live World Model refresh
        if any(p in t for p in ("update the world", "refresh the globe", "update the globe",
                                "refresh the world", "world model", "what's happening in the world",
                                "scan the world")):
            self.speak("One moment, sir — scanning the world.")
            threading.Thread(target=self._refresh_world, daemon=True).start()
            _reply("The globe's updating with the latest, sir — AI, markets, world and politics.")
            return True

        # Integrations tab
        if any(p in t for p in ("show integrations", "show me integrations", "my integrations",
                                "what are you connected to", "what's connected", "integrations tab",
                                "what apps are connected")):
            self._on_request_integrations()
            _reply("Here are my integrations, sir — what's connected and what's waiting on a key.")
            return True

        # Generate media — Alfred engineers the prompt + creates it via Replicate
        m = re.match(r"(?:alfred[,\s]*)?(?:generate|make|create|draw|render)\s+(?:me\s+)?"
                     r"(?:a|an)?\s*(image|picture|photo|art|logo|poster|video|clip|animation)\s+"
                     r"(?:of|for|about|showing|with)?\s*(.+)", t)
        if m:
            kind = "video" if m.group(1) in ("video", "clip", "animation") else "image"
            brief = m.group(2).strip()
            _reply(replicate_generate({"request": brief, "type": kind}, player=self.ui, jarvis=self))
            return True

        # Higgsfield — no API, so Alfred engineers the prompt + opens the site to paste.
        if "higgsfield" in t:
            brief = re.sub(r"(?:alfred[,\s]*)?(?:on\s+|in\s+|using\s+|with\s+|open\s+)?higgsfield\b",
                           "", t, count=1).strip(" ,:-") or text
            _reply(higgsfield_tool({"request": brief}, player=self.ui, jarvis=self))
            return True

        # Prompt engineer — Alfred WRITES the prompt himself (Higgsfield, Lovable, image…)
        m = re.match(r"(?:alfred[,\s]*)?(?:write|craft|engineer|make|compose|give me)\s+"
                     r"(?:me\s+)?(?:a|an|the)?\s*(.*?)\bprompt\b(?:\s+(?:for|to|about|that)\s+(.+))?$", t)
        if m and "prompt" in t:
            target_hint = (m.group(1) or "").strip()
            brief = (m.group(2) or target_hint or text).strip()
            _reply(prompt_engineer({"request": brief, "target": target_hint},
                                   player=self.ui, jarvis=self))
            return True

        # Instagram — "post to insta/instagram …" (photo from 📎 attachment, a path,
        # or the newest generated/Desktop image). Goes through the confirm guardrail.
        m = re.match(r"(?:alfred[,\s]*)?post\s+(?:this\s+|that\s+|it\s+)?(?:on|to)\s+"
                     r"(?:insta(?:gram)?|ig)\b[:,]?\s*(.*)", t)
        if m or ("instagram" in t and any(v in t for v in ("post", "upload", "share"))):
            caption = (m.group(1).strip() if m else
                       re.sub(r".*?(?:instagram|insta|ig)\b[:,]?", "", t, count=1).strip())
            _reply(self._execute_tool("instagram", {"caption": caption}))
            return True

        # One-shot: Echo writes content about X AND immediately offers to post it.
        m = re.match(r"(?:alfred[,\s]*)?(?:tweet|write a tweet|compose a tweet|post a tweet|"
                     r"draft and post|write and post)\s+(?:about\s+|that\s+|saying\s+)?(.+)", t)
        if m:
            topic = m.group(1).strip()
            from actions.agents import echo
            draft = echo(topic)                       # Echo writes it (Qwen3) + logs it
            _reply(self._execute_tool("twitter", {"text": draft["content"]}))  # → confirm guardrail
            return True

        # Research / current info — explicit command OR any time-sensitive question.
        # The model's training is stale, so when in doubt we search rather than guess.
        CURRENT = ("happen", "happened", "latest", "newest", "news", "today", "recent",
                   "right now", "current", "currently", "this week", "this month", "this year",
                   "who won", "did ", "score", "result", "released", "release date", "announced",
                   "2025", "2026", "viral", "trending", "update on", "yesterday", "price of",
                   "stock", "election", "won the", "white house", "just happened")
        m = re.match(r"(?:alfred[,\s]*)?(?:research|look up|find out about|search for|"
                     r"get me the latest on|what's the latest on|google|search)\s+(.+)", t)
        if m:
            q = self._clean_query(m.group(1))
        elif any(c in t for c in CURRENT):
            q = self._clean_query(text)
        else:
            q = None
        if q:
            # Research/news takes ~5s (live web search). A single, deliberate ack the
            # instant the command lands covers the wait so it never feels dead.
            self.speak("One moment, sir — pulling the latest.")
            _reply(run_workflow({"name": "research", "query": q}, player=self.ui, jarvis=self))
            return True

        # TAKE ME TO / GO TO / TRAVEL TO <place> → real map with a smooth fly-to.
        # Deterministic (not left to the LLM, which was rendering a 3D blob).
        m = re.match(r"(?:alfred[,\s]*)?(?:take me to|travel to|fly to|bring me to|go to)\s+"
                     r"(?:the\s+)?(.+)", t)
        if m:
            place = m.group(1).strip().rstrip("?.!,")
            _NOT_PLACES = ("sleep", "bed", "work", "the gym", "settings", "the next",
                           "the previous", "the top", "the bottom", "the store", "hell")
            if place and place not in _NOT_PLACES and len(place) < 40:
                from actions.visualize import _geocode, _gen_map
                if _geocode(place):                        # only if it's a REAL place
                    self.ui.show_visualization(_gen_map(place))
                    _reply(f"Taking you to {place.title()}, sir.")
                    return True

        # Visualization — render an interactive visual with no perceptible delay.
        # Runs AFTER research so news-style queries still route to search.
        VIZ_START = ("show me a", "show me an", "show me the", "show a ", "show an ",
                     "visualize", "visualise", "render a", "render an", "render the",
                     "render me", "draw me", "draw a", "draw the", "diagram of",
                     "timeline of", "explain ")
        VIZ_HAS   = ("your architecture", "how you're built", "how you are built",
                     "solar system", "show my architecture", "map of", "3d map", "fly to")
        VIZ_BLOCK = ("task", "remind", "open ", "play ", "email", "calendar",
                     "note", "weather", "message", "send ")
        if (any(t.startswith(p) for p in VIZ_START) or any(h in t for h in VIZ_HAS)) \
                and not any(b in t for b in VIZ_BLOCK):
            _reply(visualize({"request": text}, player=self.ui, jarvis=self))
            return True

        return False

    def _process_message(self, user_text: str) -> None:
        """
        Full turn: user_text → LLM stream → TTS (overlapped) → tool execution

        Streaming TTS: sentence events are piped to the TTS queue AS they
        arrive from the LLM, so Kokoro starts synthesising sentence 1 while
        the LLM is still generating sentence 2.  This cuts perceived latency
        from (LLM_total + TTS_total) down to roughly max(LLM_total, TTS_total).

        Tool-call responses never emit sentence events, so the TTS overlap
        only kicks in for pure conversational replies — which is exactly when
        it matters most.
        """
        # ── ACCESS GATE ── if the boot face check failed, this is not Ali. Alfred
        # answers nothing until the operator is verified. Unlock = pass the scan,
        # or speak the recovery phrase (a false negative must never brick him).
        if getattr(self, "_locked", False):
            self._handle_locked(user_text)
            return
        # Standalone attachment (no typed text): jump straight to reading the file.
        _has_att = bool(getattr(self.ui, "_attachment", None))
        if not user_text.strip() and not _has_att:
            return                                   # nothing to do
        # Typing at the Mac wakes him from night watch (keyboard = the user is here).
        # Telegram messages (_silent) do NOT stand the watch down — you can query him
        # from bed and the watchers keep running.
        if self._night_mode and not self._silent:
            from actions.nightwatch import end_night
            end_night(self, reason="woken", with_digest=False)
            self.ui.write_log("SYS: 🌙 Night watch stood down — he's awake.")
        self.ui.set_state("THINKING")
        if user_text.strip():
            self.ui.write_log(f"You: {user_text}")
        self._responding = True   # half-duplex: mute mic until this reply finishes (voice + typed)
        self._barged = False      # fresh turn — clear any prior interrupt flag
        self._cur_user = user_text

        # Growth: if this is a correction, learn the lesson from the previous request.
        try:
            from core.learning import detect_correction, add_lesson
            if detect_correction(user_text):
                _prev = next((m["content"] for m in reversed(self._conversation)
                              if m.get("role") == "user"), "")
                if _prev:
                    add_lesson(_prev, user_text)
                    self.ui.write_log("SYS: 🧠 Lesson learned.")
        except Exception:
            pass

        # Resolve a pending destructive action awaiting confirmation.
        if self._pending_action is not None:
            name, args, desc = self._pending_action
            self._pending_action = None
            if self._is_affirmative(user_text):
                args = {**args, "_confirmed": True}
                self.ui.write_log(f"SYS: Confirmed — {desc}")
                result = self._execute_tool(name, args)
                self._conversation.append({"role": "assistant", "content": result})
                self.ui.write_log(f"Alfred: {result}")
                self.speak(result)
                # a task-chain paused for this confirmation? — resume the rest now
                if getattr(self, "_chain_resume", None):
                    rest, self._chain_resume = self._chain_resume, None
                    self._run_chain(rest, announced=True)
            elif self._is_negative(user_text):
                self.ui.write_log("SYS: Cancelled.")
                self._chain_resume = None
                self.speak("Cancelled, sir.")
            else:
                # not a clear yes/no — abandon the pending action and treat as new input
                self.speak("I'll hold off on that, sir.")
            self._finish_turn()
            return

        # ── HUMAN MOMENT — checked before ANY routing (chain-splitter included, so a
        # sentence about how he's doing can never be chopped into "tasks"). No tools.
        if self._is_personal((user_text or "").lower()):
            def _r(msg: str) -> None:
                self._conversation.append({"role": "user", "content": user_text})
                self._conversation.append({"role": "assistant", "content": msg})
                self.ui.write_log(f"Alfred: {msg}")
                self.speak(msg)
            self._talk_like_a_person(user_text, _r)
            self._finish_turn()
            return

        # DEEP MULTITASKING — a compound request ("research X, then draft a tweet and
        # add a mission") is split into steps and executed in sequence, one turn.
        _steps = self._split_chain(user_text)
        if _steps:
            self._run_chain(_steps)
            self._finish_turn()
            return

        if self._fast_path(user_text):
            self._finish_turn()
            return

        # No filler for ordinary replies — Haiku answers in ~1s, and a filler on every
        # turn is what made Alfred say "let me see / on it sir" constantly. Fillers are
        # reserved for genuinely slow ops (research, briefing, visualization).

        # A staged file attachment (📎): answer about it directly with the heavy
        # model. This is robust for large docs and works standalone (no typed text),
        # and it avoids the tool/stream path that returned an OpenAI-compatible error
        # on big payloads.
        try:
            att = self.ui.take_attachment() if hasattr(self.ui, "take_attachment") else None
        except Exception:
            att = None
        if att:
            self._answer_about_file(att, user_text)
            self._finish_turn()
            return

        self._conversation.append({"role": "user", "content": user_text})

        MAX_HISTORY = 10
        if len(self._conversation) > MAX_HISTORY:
            self._conversation = self._conversation[-MAX_HISTORY:]

        # Replayed history must be valid for the strict cloud endpoint. Blind
        # truncation can orphan a tool-result from its tool-call → HTTP 400. Tool
        # mechanics don't need to persist as context, so strip them on replay.
        messages = [
            {"role": "system", "content": self._build_system_prompt()}
        ] + self._sanitize_history(self._conversation)

        # Tools whose output needs a second LLM round to summarise/interpret.
        # Everything else returns a user-ready string → speak directly.
        _NEEDS_LLM_ROUND = {"web_search", "screen_process", "agent_task"}

        MAX_TOOL_ROUNDS = 6
        for _round in range(MAX_TOOL_ROUNDS):
            if self._barged:
                break                      # user cut in — abandon this reply entirely
            final_content    = ""
            final_tool_calls: list = []
            # Sentences already queued to TTS during streaming (may be empty
            # for tool-call rounds where the model emits no content).
            _streamed: list[str] = []

            try:
                for event in call_llm_stream(messages, OLLAMA_TOOLS):
                    if self._barged:
                        break              # stop queuing speech the moment he's interrupted
                    if event["type"] == "sentence":
                        # ── Overlap TTS with LLM generation ─────────────────
                        # Queue this sentence immediately; the TTS worker
                        # synthesises it while the LLM is still generating
                        # the next one.
                        _streamed.append(event["text"])
                        self.speak(event["text"])
                    elif event["type"] == "done":
                        final_content    = event["content"]
                        final_tool_calls = event["tool_calls"]
            except RuntimeError as e:
                self.speak_error("LLM", e)
                return

            if self._barged:
                break            # interrupted mid-reply — don't speak/act on the rest

            # ── No tool calls: pure conversational reply ─────────────────────
            if not final_tool_calls:
                if _streamed:
                    # Sentences already queued to TTS — just update history/log.
                    assistant_msg = {"role": "assistant", "content": final_content}
                    messages.append(assistant_msg)
                    self._conversation.append(assistant_msg)
                    self.ui.write_log(f"Alfred: {final_content}")
                elif final_content:
                    # Very short response (no sentence boundary) — speak now.
                    assistant_msg = {"role": "assistant", "content": final_content}
                    messages.append(assistant_msg)
                    self._conversation.append(assistant_msg)
                    self.ui.write_log(f"Alfred: {final_content}")
                    self.speak(final_content)
                break

            # ── Tool calls present ────────────────────────────────────────────
            assistant_msg = {
                "role":       "assistant",
                "content":    final_content or "",
                "tool_calls": final_tool_calls,
            }
            messages.append(assistant_msg)
            self._conversation.append(assistant_msg)

            # ── Fast path: save_memory + verbal content in same round ────────
            _only_memory = all(
                tc.get("function", {}).get("name") == "save_memory"
                for tc in final_tool_calls
            )
            if _only_memory and final_content:
                for tc in final_tool_calls:
                    fn    = tc.get("function", {})
                    targs = fn.get("arguments", {})
                    if isinstance(targs, str):
                        try:
                            targs = json.loads(targs)
                        except Exception:
                            targs = {}
                    self._execute_tool("save_memory", targs)
                assistant_msg2 = {"role": "assistant", "content": final_content}
                messages.append(assistant_msg2)
                self._conversation.append(assistant_msg2)
                self.ui.write_log(f"Alfred: {final_content}")
                if not _streamed:
                    self.speak(final_content)
                break

            # ── Execute tools ─────────────────────────────────────────────────
            all_silent    = True
            _tool_results: list[tuple[str, str]] = []

            for tc in final_tool_calls:
                fn    = tc.get("function", {})
                tname = fn.get("name", "")
                targs = fn.get("arguments", {})
                if isinstance(targs, str):
                    try:
                        targs = json.loads(targs)
                    except Exception:
                        targs = {}

                tc_id = tc.get("id", "")
                self.ui.write_log(f"SYS: ▶ {tname}")
                result = self._execute_tool(tname, targs)

                if result != "__SILENT__":
                    all_silent = False
                    _tool_results.append((tname, result))

                tool_msg: dict = {
                    "role":    "tool",
                    "content": "Done." if result == "__SILENT__" else str(result),
                }
                if tc_id:
                    tool_msg["tool_call_id"] = tc_id

                messages.append(tool_msg)
                self._conversation.append(tool_msg)

            # ── Fast-ack: every call was save_memory (silent) ────────────────
            if all_silent:
                _saved_name: str | None = None
                for _tc in final_tool_calls:
                    _fn = _tc.get("function", {})
                    if _fn.get("name") == "save_memory":
                        _a = _fn.get("arguments", {})
                        if isinstance(_a, str):
                            try:
                                _a = json.loads(_a)
                            except Exception:
                                _a = {}
                        if isinstance(_a, dict) and _a.get("key") == "name" and _a.get("value"):
                            _saved_name = str(_a["value"])
                            break
                _ack = f"Got it, {_saved_name}." if _saved_name else "Noted."
                _amsg = {"role": "assistant", "content": _ack}
                messages.append(_amsg)
                self._conversation.append(_amsg)
                self.ui.write_log(f"Alfred: {_ack}")
                self.speak(_ack)
                break

            # ── Direct-result: speak tool output, skip LLM round ────────────
            if _tool_results and not any(n in _NEEDS_LLM_ROUND for n, _ in _tool_results):
                _, _reply = _tool_results[-1]
                _amsg = {"role": "assistant", "content": _reply}
                messages.append(_amsg)
                self._conversation.append(_amsg)
                self.ui.write_log(f"Alfred: {_reply}")
                self.speak(_reply)
                break

        self._finish_turn()   # reopen mic only after the whole reply has finished playing

    # ------------------------------------------------------------------
    # STT listening loops
    # ------------------------------------------------------------------

    def _listen_whisper(self) -> None:
        """Mic → VAD → Whisper → LLM loop."""
        vad = _VADBuffer()
        q: queue.Queue = queue.Queue(maxsize=200)

        def callback(indata, frames, time_info, status):
            with self._speaking_lock:
                spk = self._speaking
            typing = getattr(self.ui, "typing", False)
            # Half-duplex: ignore the mic for the whole turn (ack → think → speak → tail)
            # so Alfred never hears his own voice and acts on a phantom command.
            # Also ignore it while the user is typing (keyboard noise / ambient).
            gated = (spk or self._responding or time.time() < self._mic_blackout_until or typing)
            if not gated and not self.ui.muted:
                try:
                    q.put_nowait(indata.copy())
                except queue.Full:
                    pass
            elif self._barge_enabled and spk and not self.ui.muted and not typing:
                # Alfred is speaking → feed the side listener, which only watches
                # for an interrupt word and cuts him off (never runs a command).
                try:
                    self._interrupt_q.put_nowait(indata.copy())
                except queue.Full:
                    pass

        threading.Thread(target=self._interrupt_loop, daemon=True).start()
        try:
            with sd.InputStream(
                samplerate=SAMPLE_RATE_IN,
                channels=CHANNELS,
                dtype="float32",
                blocksize=BLOCK_SIZE,
                callback=callback,
            ):
                self.ui.write_log("SYS: Mic active (Whisper STT) · barge-in on.")
                while True:
                    try:
                        chunk = q.get(timeout=0.1)
                        audio = vad.process(chunk.flatten())
                        if audio is not None:
                            if self._stt is None:          # STT failed to load — don't crash
                                self._responding = False   # the loop; typing still works
                                continue
                            # Go half-duplex for the whole turn: stop listening NOW so
                            # the ack/reply can't be re-heard and acted on.
                            self._responding = True
                            self.ui.set_state("THINKING")
                            # No pre-recorded filler — it was the source of every
                            # "Right away/on it sir" echo and the flood. Cached/pre-warmed
                            # TTS makes replies fast enough that no ack clip is needed.
                            with self._stt_lock:
                                text = self._stt.transcribe(audio)
                            self._last_audio = audio       # for the voice-print lock
                            # NIGHT WATCH: wake-word-only. Everything without "alfred"
                            # is ignored (TV, notifications, room noise). His name
                            # snaps him back to normal mode.
                            if self._night_mode:
                                if "alfred" in (text or "").lower():
                                    from actions.nightwatch import end_night
                                    end_night(self, reason="woken", with_digest=False)
                                    rest = re.sub(r".*?\balfred\b[,\s]*", "", text,
                                                  flags=re.IGNORECASE).strip()
                                    self.ui.write_log("SYS: 🌙 Night watch stood down — he's awake.")
                                    if len(rest) > 3:
                                        self._process_message(rest)
                                    else:
                                        self.speak("Evening, sir — couldn't sleep? I'm here.")
                                        self._finish_turn()
                                else:
                                    self._responding = False
                                vad.reset()
                                try:
                                    while True:
                                        q.get_nowait()
                                except queue.Empty:
                                    pass
                                continue
                            if self._is_real_command(text):
                                self._process_message(text)   # ends with _finish_turn()
                            else:
                                # garbage / echo / filler — never act on it; reopen mic
                                self._responding = False
                                self._mic_blackout_until = time.time() + 0.4
                            # Flush everything captured during the response so Alfred's own
                            # voice / room echo can't start a phantom turn (the flood).
                            vad.reset()
                            try:
                                while True:
                                    q.get_nowait()
                            except queue.Empty:
                                pass
                    except queue.Empty:
                        pass
        except Exception as e:
            print(f"[STT-Whisper] Mic error: {e}")
            traceback.print_exc()
            self.ui.write_log(
                f"ERR: Microphone — {e}\n"
                "     Check: microphone plugged in, OS privacy permissions granted, "
                "correct audio device selected in system settings."
            )

    def _listen_vosk(self) -> None:
        """Mic → Vosk streaming → LLM loop."""
        q: queue.Queue = queue.Queue(maxsize=200)

        def callback(indata, frames, time_info, status):
            with self._speaking_lock:
                is_speaking = self._speaking
            # Half-duplex: ignore the mic for the whole turn (ack → think → speak → tail)
            # so Alfred never hears his own voice and acts on a phantom command.
            # Also ignore it while the user is typing (keyboard noise / ambient).
            if (self._responding or time.time() < self._mic_blackout_until
                    or getattr(self.ui, "typing", False)):
                is_speaking = True
            if not is_speaking and not self.ui.muted:
                try:
                    q.put_nowait(indata.copy())
                except queue.Full:
                    pass

        try:
            with sd.InputStream(
                samplerate=SAMPLE_RATE_IN,
                channels=CHANNELS,
                dtype="int16",
                blocksize=4096,
                callback=callback,
            ):
                self.ui.write_log("SYS: Mic active (Vosk STT).")
                while True:
                    try:
                        chunk = q.get(timeout=0.1)
                        text, is_final = self._stt.process_chunk(chunk.tobytes())
                        if is_final and text.strip():
                            self._process_message(text)
                    except queue.Empty:
                        pass
        except Exception as e:
            print(f"[STT-Vosk] Mic error: {e}")
            traceback.print_exc()
            self.ui.write_log(
                f"ERR: Microphone — {e}\n"
                "     Check: microphone plugged in, OS privacy permissions granted, "
                "correct audio device selected in system settings."
            )

    # ------------------------------------------------------------------
    # Text command loop (UI input box)
    # ------------------------------------------------------------------

    def _text_command_loop(self) -> None:
        while True:
            try:
                text = self._text_queue.get(timeout=0.5)
                if text.strip():
                    self._process_message(text)
            except queue.Empty:
                pass

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def run(self) -> None:
        """
        Startup strategy — optimised for minimum time-to-interactive:

        1. LLM warmup + STT load  →  parallel, fast (~3s)
        2. TTS load               →  parallel, slow (~20s for Kokoro)
        3. Wait only for (1)      →  go online immediately
        4. TTS finishes in BG     →  queued speech plays automatically
        """
        try:
            self.ui.on_reconfigure = self.reconfigure

            # ── Ollama ────────────────────────────────────────────────────
            from core.llm_client import ensure_ollama_running, warmup_model, check_model_available, get_llm_provider
            _is_cloud = get_llm_provider() == "openai"
            self.ui.write_log("SYS: Connecting to LLM endpoint…" if _is_cloud else "SYS: Checking Ollama…")
            if ensure_ollama_running():
                self.ui.write_log("SYS: LLM endpoint OK." if _is_cloud else "SYS: Ollama OK.")
                check_model_available(log=self.ui.write_log)
            else:
                self.ui.write_log("ERR: LLM endpoint unreachable — check llm_url / API key" if _is_cloud else "ERR: Ollama unavailable — run: ollama serve")

            # ── Config ────────────────────────────────────────────────────
            stt_engine   = self._config.get("stt_engine",   "whisper").lower()
            stt_language = self._config.get("stt_language", "auto")
            stt_model    = self._config.get("stt_model",    "base")
            tts_engine   = self._config.get("tts_engine",   "edgetts").lower()

            # ── Startup progress panel ────────────────────────────────────
            self.ui.show_startup_panel()

            _warmup_done = threading.Event()
            _stt_done    = threading.Event()

            # ── LLM warmup thread ─────────────────────────────────────────
            def _do_warmup():
                try:
                    # Pass the STATIC system prompt so Ollama evaluates and caches
                    # its KV state during startup.  Real requests start with the same
                    # static prefix → Ollama reuses cached KV → first token <1 s
                    # instead of the ~17 s it takes to re-evaluate 300+ tokens cold.
                    static_prompt = _load_system_prompt()
                    ok = warmup_model(system_prompt=static_prompt)
                    if ok:
                        self.ui.write_log("SYS: LLM ready.")
                    else:
                        # warmup() returned False — check if model is missing
                        from core.llm_client import check_model_available
                        if not check_model_available(log=self.ui.write_log):
                            pass   # warning already written by check_model_available
                        else:
                            self.ui.write_log("WRN: LLM warmup failed — check LLM endpoint / API key")
                    # GPU usage tip (cross-platform, non-blocking)
                    try:
                        import torch
                        if torch.cuda.is_available():
                            self.ui.write_log(f"SYS: GPU detected — {torch.cuda.get_device_name(0)}")
                        else:
                            self.ui.write_log(
                                "WRN: CUDA not detected — LLM running on CPU (slow). "
                                "For GPU speed install CUDA PyTorch: "
                                "pip install torch --index-url https://download.pytorch.org/whl/cu118"
                            )
                    except Exception:
                        pass   # torch not installed or not applicable
                    self.ui.mark_startup_ready("llm")
                except Exception as e:
                    self.ui.write_log(f"ERR: LLM warmup — {e}")
                    self.ui.mark_startup_ready("llm", error=True)
                finally:
                    _warmup_done.set()

            # ── STT load thread ───────────────────────────────────────────
            def _do_stt():
                try:
                    self.ui.write_log(f"SYS: Loading {stt_engine.upper()} STT…")
                    if stt_engine == "vosk":
                        from core.stt import VoskSTT
                        self._stt = VoskSTT(
                            self._config.get("vosk_model_path"),
                            language=stt_language,
                        )
                    else:
                        from core.stt import WhisperSTT
                        self._stt = WhisperSTT(stt_model, language=stt_language)
                    self.ui.write_log("SYS: STT ready.")
                    self.ui.mark_startup_ready("stt")
                except Exception as e:
                    self.ui.write_log(f"ERR: STT — {e}")
                    self.ui.mark_startup_ready("stt", error=True)
                finally:
                    _stt_done.set()

            # ── TTS load thread — does NOT block going online ─────────────
            def _do_tts():
                try:
                    self.ui.write_log(f"SYS: Loading {tts_engine.upper()} TTS…")
                    if tts_engine == "kokoro":
                        self.ui.write_log("SYS: Kokoro — loading model + compiling JIT…")
                    from core.tts import create_tts_player
                    self._tts = create_tts_player(self._config)
                    self._tts_ready.set()          # unblock _tts_worker
                    self._prewarm_tts()            # cache canned demo answers for instant speech
                    self.ui.write_log("SYS: TTS ready.")
                    self.ui.mark_startup_ready("tts")
                    self.ui.set_startup_status("● All systems ready.")
                    self.ui.hide_startup_panel()
                    self.speak("Alfred fully online.")
                except Exception as e:
                    import traceback as _tb; _tb.print_exc()
                    self.ui.write_log(f"ERR: TTS — {e}")
                    self.ui.mark_startup_ready("tts", error=True)
                    self._tts_ready.set()

            # Launch all three simultaneously
            self.ui.write_log("SYS: Loading systems in parallel…")
            threading.Thread(target=_do_warmup, daemon=True).start()
            threading.Thread(target=_do_stt,    daemon=True).start()
            threading.Thread(target=_do_tts,    daemon=True).start()

            # ── Wait ONLY for STT + LLM (fast) ────────────────────────────
            _warmup_done.wait(timeout=60)
            _stt_done.wait(timeout=60)

            # ── Go online immediately ──────────────────────────────────────
            self.ui.write_log("SYS: ALFRED online.")
            self.ui.set_state("LISTENING")
            self.ui.set_startup_status("● ALFRED online · Voice loading in background…")

            # Push the Obsidian-derived profile to the MEMORY panel (web UI).
            try:
                from core.obsidian import load_profile
                _prof = load_profile()
                if hasattr(self.ui, "set_memory") and _prof.get("bullets"):
                    self.ui.set_memory(len(_prof["bullets"]), _prof["bullets"][:8])
                    self.ui.write_log(f"SYS: Loaded {len(_prof['bullets'])} facts from Obsidian vault.")
            except Exception:
                pass

            threading.Thread(target=self._tts_worker,        daemon=True).start()
            threading.Thread(target=self._text_command_loop,  daemon=True).start()

            # STT loop — blocks this thread forever
            if stt_engine == "vosk":
                self._listen_vosk()
            else:
                self._listen_whisper()

        except Exception as e:
            self.ui.write_log(f"ERR: Init failed — {e}")
            traceback.print_exc()


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------

def main() -> None:
    # ── Pre-import torch in background immediately ─────────────────────────
    # By the time the TTS thread starts (~5s from now), torch will already
    # be in sys.modules — removing it from the TTS critical path entirely.
    def _preload_torch():
        try:
            import torch  # noqa: F401  (side-effect import only)
        except Exception:
            pass
    threading.Thread(target=_preload_torch, daemon=True).start()
    # ───────────────────────────────────────────────────────────────────────

    try:
        from core.web_ui import AlfredWebUI
        ui = AlfredWebUI("face.png")
        print("[UI] Alfred web dashboard loaded.")
    except Exception as _ui_err:
        import traceback as _tb; _tb.print_exc()
        print(f"[UI] Web UI unavailable ({_ui_err}) — falling back to HUD.")
        ui = JarvisUI("face.png")

    def runner():
        # 1. Wait until the user completes the setup overlay (first run)
        #    or config already exists (subsequent runs).
        ui.wait_for_api_key()

        # 2. Install any missing engine packages before loading engines.
        #    Progress is streamed to the log panel in real time.
        ui.write_log("SYS: Checking dependencies…")
        cfg = _load_config()
        _install_done = threading.Event()

        def _do_install():
            try:
                from core.installer import install_for_config
                install_for_config(cfg, log=ui.write_log)
            except Exception as e:
                ui.write_log(f"ERR: Dependency install — {e}")
            finally:
                _install_done.set()

        threading.Thread(target=_do_install, daemon=True).start()
        _install_done.wait()   # blocks runner thread; UI remains responsive

        # 3. Start the assistant (loads STT / TTS / LLM).
        jarvis = JarvisLocal(ui)
        try:
            jarvis.run()
        except KeyboardInterrupt:
            print("\n[Alfred] Shutting down…")

    threading.Thread(target=runner, daemon=True).start()
    ui.root.mainloop()

if __name__ == "__main__":
    main()
