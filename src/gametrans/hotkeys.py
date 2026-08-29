"""Global hotkeys.

Optional: the app is fully usable without them, but reaching for a config file
mid-game is not realistic, so `keyboard` is used when it is installed. It
registers a low-level Windows hook, which is what lets the keys still fire while
a fullscreen game has focus.
"""

from __future__ import annotations

import logging
import platform
from typing import Callable, Dict, List, Optional

from .config import HotkeyConfig

log = logging.getLogger(__name__)


class HotkeyManager:
    """Registers global hotkeys, degrading to a no-op when unavailable."""

    def __init__(self, cfg: HotkeyConfig) -> None:
        self.cfg = cfg
        self._handles: List[object] = []
        self._keyboard = None
        self.available = False
        self.reason = ""

        if not cfg.enabled:
            self.reason = "disabled in config"
            return

        try:
            import keyboard  # type: ignore

            self._keyboard = keyboard
            self.available = True
        except ImportError:
            self.reason = "install with `pip install gametrans[hotkeys]`"
        except Exception as exc:  # pragma: no cover - platform specific
            self.reason = str(exc)

    def register(self, bindings: Dict[str, Optional[Callable[[], None]]]) -> None:
        """Bind {hotkey_string: callback}. Unbound or empty entries are skipped."""
        if not self.available or self._keyboard is None:
            if self.reason:
                log.info("Global hotkeys unavailable (%s)", self.reason)
            return

        for combo, callback in bindings.items():
            if not combo or callback is None:
                continue
            try:
                handle = self._keyboard.add_hotkey(combo, callback, suppress=False)
                self._handles.append(handle)
                log.info("hotkey registered: %s", combo)
            except Exception as exc:
                # On Windows a permission error usually means the game is running
                # elevated; say so rather than failing silently.
                log.warning("could not register hotkey %s: %s", combo, exc)
                if platform.system() == "Windows" and "access" in str(exc).lower():
                    log.warning(
                        "Run gametrans as administrator if the game is elevated, "
                        "otherwise its window swallows the hook."
                    )

    def unregister_all(self) -> None:
        if self._keyboard is None:
            return
        for handle in self._handles:
            try:
                self._keyboard.remove_hotkey(handle)
            except Exception:  # pragma: no cover - shutdown best effort
                pass
        self._handles.clear()
