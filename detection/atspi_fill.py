"""AT-SPI based form filling using the accessibility tree."""

from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class FormField:
    """A discovered form field from the AT-SPI tree."""

    role: str
    name: str
    obj: object  # Atspi.Accessible


class AtspiInspector:
    """Walk the AT-SPI accessibility tree to find and fill form fields."""

    def __init__(self) -> None:
        self._atspi = None

    def _import_atspi(self):
        """Lazily import gi.repository.Atspi."""
        if self._atspi is not None:
            return
        import gi
        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi
        self._atspi = Atspi

    @staticmethod
    def is_available() -> bool:
        """Check if AT-SPI is usable."""
        try:
            import gi
            gi.require_version("Atspi", "2.0")
            from gi.repository import Atspi
            desktop = Atspi.get_desktop(0)
            return desktop is not None
        except Exception:
            return False

    def find_app(self, app_name: str) -> object | None:
        """Find an application in the AT-SPI tree by name."""
        self._import_atspi()
        desktop = self._atspi.get_desktop(0)
        for i in range(desktop.get_child_count()):
            app = desktop.get_child_at_index(i)
            if app and app_name.lower() in (app.get_name() or "").lower():
                return app
        return None

    def find_fields(self, root) -> list[FormField]:
        """Recursively find text entry and password fields under the root."""
        fields: list[FormField] = []
        self._walk(root, fields)
        return fields

    def _walk(self, node, fields: list[FormField]) -> None:
        """Recursively walk the AT-SPI tree."""
        if node is None:
            return

        try:
            role = node.get_role()
        except Exception:
            return

        Atspi = self._atspi
        role_name = node.get_role_name() or ""

        if role in (Atspi.Role.ENTRY, Atspi.Role.TEXT, Atspi.Role.PASSWORD_TEXT):
            fields.append(FormField(
                role=role_name,
                name=node.get_name() or "",
                obj=node,
            ))
        elif role == Atspi.Role.PUSH_BUTTON:
            name = (node.get_name() or "").lower()
            if name in ("log in", "login", "sign in", "submit"):
                fields.append(FormField(
                    role=role_name,
                    name=node.get_name() or "",
                    obj=node,
                ))

        try:
            count = node.get_child_count()
        except Exception:
            return

        for i in range(count):
            try:
                child = node.get_child_at_index(i)
                self._walk(child, fields)
            except Exception:
                continue

    def fill_field(self, field: FormField, text: str) -> bool:
        """Set text contents of a field."""
        try:
            iface = field.obj.get_editable_text_iface()
            if iface:
                # Clear existing text
                current_len = field.obj.get_text(0, -1)
                if current_len:
                    iface.delete_text(0, len(current_len))
                iface.insert_text(0, text, len(text))
                return True

            # Fallback: try set_text_contents via text interface
            text_iface = field.obj.get_text_iface()
            if text_iface and hasattr(field.obj, "set_text_contents"):
                field.obj.set_text_contents(text)
                return True
        except Exception:
            log.debug("Failed to fill field %s via AT-SPI", field.name, exc_info=True)
        return False

    def click_button(self, field: FormField) -> bool:
        """Click a button via AT-SPI action interface."""
        try:
            action = field.obj.get_action_iface()
            if action:
                for i in range(action.get_n_actions()):
                    if action.get_action_name(i) in ("click", "press", "activate"):
                        action.do_action(i)
                        return True
        except Exception:
            log.debug("Failed to click button %s", field.name, exc_info=True)
        return False

    def fill_login_form(
        self,
        app_name: str,
        username: str,
        password: str,
        field_order: list[str] | None = None,
        auto_submit: bool = False,
    ) -> bool:
        """Find and fill the login form for an application.

        Returns True if fields were successfully filled.
        """
        if field_order is None:
            field_order = ["username", "password"]

        app = self.find_app(app_name)
        if app is None:
            log.warning("AT-SPI: application %r not found", app_name)
            return False

        fields = self.find_fields(app)
        if not fields:
            log.warning("AT-SPI: no form fields found for %r", app_name)
            return False

        # Separate entry fields and buttons
        entries = [f for f in fields if f.role in ("entry", "text", "password text")]
        buttons = [f for f in fields if f.role == "push button"]

        if len(entries) < 2:
            log.warning("AT-SPI: expected at least 2 entry fields, found %d", len(entries))
            return False

        # Fill fields in order
        values = {"username": username, "password": password}
        for i, field_name in enumerate(field_order):
            if i < len(entries) and field_name in values:
                if not self.fill_field(entries[i], values[field_name]):
                    log.warning("AT-SPI: failed to fill field %d (%s)", i, field_name)
                    return False

        if auto_submit and buttons:
            self.click_button(buttons[0])

        return True

    def dump_tree(self, app_name: str) -> str:
        """Dump the AT-SPI tree for debugging. Returns formatted string."""
        app = self.find_app(app_name)
        if app is None:
            return f"Application '{app_name}' not found in AT-SPI tree."

        lines: list[str] = []
        self._dump_node(app, 0, lines)
        return "\n".join(lines)

    def _dump_node(self, node, depth: int, lines: list[str]) -> None:
        if node is None:
            return

        indent = "  " * depth
        try:
            role = node.get_role_name() or "unknown"
            name = node.get_name() or ""
            state_set = node.get_state_set()
            lines.append(f"{indent}[{role}] {name!r}")
        except Exception:
            lines.append(f"{indent}[error reading node]")
            return

        try:
            count = node.get_child_count()
        except Exception:
            return

        for i in range(count):
            try:
                child = node.get_child_at_index(i)
                self._dump_node(child, depth + 1, lines)
            except Exception:
                continue
