# Contributing

Open Computer is an early Windows desktop-control toolkit. Small changes with a
clear user-visible purpose and reproducible validation are welcome.

Use Windows and Python 3.11 or newer with Tkinter. From a source checkout:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m opencomputer.app --demo
```

The normal tests use fake inputs. Native smoke tests send real input only to the
disposable fixture they create and require a deliberate `--run` invocation. Focus
that fixture manually if Windows refuses background activation. Do not run native
tests against someone else's application or an unattended desktop.

Keep the controller's safety boundaries intact: only the local panel can arm,
selection changes disarm, each action requires a fresh single-use observation,
and stop cancels in-flight input and requires a local reset. Preserve the visible
target overlay and its click-through/no-activate behavior. Keep Tk work on its
main thread and send backend activity through the event queue.

Explain what changed, why, and how it was tested in a pull request. Distinguish
fake-backend, native-fixture, and actual model-driven evidence. Add tests when
they verify new behavior or a regression; avoid tests that only mirror the code.
Do not include service tokens, screenshots of private apps, personal paths,
connection files, generated environments, or local test artifacts.

Use [SECURITY.md](SECURITY.md) for vulnerability reporting. For ordinary bugs,
include reproduction steps and sanitized error messages in a GitHub issue.
