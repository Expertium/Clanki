# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""The AnkiConnect tab of Preferences (spec ankiconnect.settings): the
on/off switch and the add-on's config.json settings (address, port, allowed
web origins, API key), with the server's current state."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from aqt.ankiconnect import AnkiConnectSettings, instance, load_settings
from aqt.ankiconnect_server import ServerState, ServerStatus
from aqt.qt import (
    QCheckBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QSpinBox,
    QVBoxLayout,
    QWidget,
    qconnect,
)
from aqt.utils import tr

if TYPE_CHECKING:
    from aqt.main import AnkiQt


def status_text(status: ServerStatus, enabled: bool) -> str:
    if not enabled or status.state == ServerState.OFF:
        return tr.preferences_ankiconnect_status_off()
    if status.state == ServerState.LISTENING:
        return tr.preferences_ankiconnect_status_listening(
            address=status.address, port=str(status.port)
        )
    if status.state == ServerState.PORT_IN_USE:
        return tr.preferences_ankiconnect_status_port_in_use(
            address=status.address, port=str(status.port)
        )
    if status.state == ServerState.ERROR:
        return tr.preferences_ankiconnect_status_error(
            address=status.address, port=str(status.port), error=status.error
        )
    return tr.preferences_ankiconnect_status_starting()


def settings_from_form(
    initial: AnkiConnectSettings,
    *,
    enabled: bool,
    address: str,
    port: int,
    origins: str,
    api_key: str,
) -> AnkiConnectSettings:
    """The tab's settings: one origin per non-empty line; an empty address
    keeps the stored one; an empty key is no key, and an untouched key field
    keeps the stored key (an empty-string key from the add-on included)."""
    initial_key = initial.api_key
    return replace(
        initial,
        enabled=enabled,
        bind_address=address.strip() or initial.bind_address,
        bind_port=port,
        cors_origins=tuple(
            line.strip() for line in origins.splitlines() if line.strip()
        ),
        api_key=initial_key if api_key == (initial_key or "") else (api_key or None),
    )


class AnkiConnectPreferences(QWidget):
    def __init__(self, mw: AnkiQt, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.mw = mw
        service = instance()
        self._initial = (
            service.settings if service is not None else load_settings(mw.pm)
        )
        settings = self._initial

        layout = QVBoxLayout(self)
        self.enabled_box = QCheckBox(tr.preferences_ankiconnect_enabled())
        self.enabled_box.setChecked(settings.enabled)
        layout.addWidget(self.enabled_box)
        explanation = QLabel(tr.preferences_ankiconnect_explanation())
        explanation.setWordWrap(True)
        layout.addWidget(explanation)

        self.server = QGroupBox(tr.preferences_ankiconnect_server())
        form = QFormLayout(self.server)
        self.address = QLineEdit(settings.bind_address)
        self.address.setToolTip(tr.preferences_ankiconnect_bind_address_tooltip())
        form.addRow(tr.preferences_ankiconnect_bind_address(), self.address)
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(settings.bind_port)
        form.addRow(tr.preferences_ankiconnect_port(), self.port)
        layout.addWidget(self.server)

        self.access = QGroupBox(tr.preferences_ankiconnect_access())
        form = QFormLayout(self.access)
        self.origins = QPlainTextEdit("\n".join(settings.cors_origins))
        self.origins.setToolTip(tr.preferences_ankiconnect_cors_origins_tooltip())
        self.origins.setMaximumHeight(90)
        form.addRow(tr.preferences_ankiconnect_cors_origins(), self.origins)
        self.api_key = QLineEdit(settings.api_key or "")
        self.api_key.setPlaceholderText(
            tr.preferences_ankiconnect_api_key_placeholder()
        )
        self.api_key.setToolTip(tr.preferences_ankiconnect_api_key_tooltip())
        form.addRow(tr.preferences_ankiconnect_api_key(), self.api_key)
        layout.addWidget(self.access)

        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        layout.addStretch()

        if service is not None:
            self._show_status(service.status)
            service.status_listeners.append(self._show_status)
        else:
            self.status.setText(tr.preferences_ankiconnect_status_off())
        qconnect(self.destroyed, self._forget_listener)

    def _show_status(self, status: ServerStatus) -> None:
        service = instance()
        enabled = service.settings.enabled if service is not None else False
        self.status.setText(status_text(status, enabled))

    def _forget_listener(self, *_: object) -> None:
        service = instance()
        if service is not None and self._show_status in service.status_listeners:
            service.status_listeners.remove(self._show_status)

    # reading back

    def settings(self) -> AnkiConnectSettings:
        return settings_from_form(
            self._initial,
            enabled=self.enabled_box.isChecked(),
            address=self.address.text(),
            port=self.port.value(),
            origins=self.origins.toPlainText(),
            api_key=self.api_key.text(),
        )

    def save(self) -> bool:
        """Apply the settings when they changed; True if they did."""
        settings = self.settings()
        if settings == self._initial:
            return False
        service = instance()
        if service is not None:
            service.apply_settings(settings)
        else:
            from aqt.ankiconnect import save_settings

            save_settings(self.mw.pm, settings)
        self._initial = settings
        return True
