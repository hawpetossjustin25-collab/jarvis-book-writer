"""The Planner tab (calendar, day plan, reminders, writing schedule) and the alarm that rings reminders."""
from __future__ import annotations

import logging
import math
import os
import struct
import wave
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

from PyQt6.QtCore import QDate, QDateTime, QObject, Qt, QTime, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QFont, QTextCharFormat
from PyQt6.QtWidgets import (QApplication, QCalendarWidget, QCheckBox, QComboBox, QDateTimeEdit, QDialog,
                             QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                             QMessageBox, QPushButton, QSpinBox, QStyle, QSystemTrayIcon, QTimeEdit, QVBoxLayout,
                             QWidget)

from jarvis_books.planner import REPEATS, SNOOZE_MINUTES, Planner, Reminder, day_bounds, describe_time

log = logging.getLogger(__name__)

RING_LIMIT_MS = 10 * 60 * 1000  # the sound stops after 10 minutes; the pop-up stays until answered


def _quiet() -> bool:
    """Tests run offscreen and never touch the real speakers."""
    return os.environ.get("QT_QPA_PLATFORM") == "offscreen"


def make_alarm_sound(path: Path) -> Path:
    """A 2-second two-tone beep pattern (written once), looped while an alarm rings."""
    if path.exists() and path.stat().st_size > 1000:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    rate, frames = 22050, bytearray()
    pattern = [(880, 0.18), (0, 0.07), (1175, 0.18), (0, 0.07)] * 2 + [(0, 1.0)]
    for freq, seconds in pattern:
        n = int(rate * seconds)
        for i in range(n):
            fade = min(1.0, i / 200, (n - i) / 200)
            v = int(0.55 * 32767 * fade * math.sin(2 * math.pi * freq * i / rate)) if freq else 0
            frames += struct.pack("<h", v)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))
    return path


class AlarmDialog(QDialog):
    snooze = pyqtSignal(int, int)
    dismiss = pyqtSignal(int)

    def __init__(self, reminder: Reminder, now: float, parent=None):
        super().__init__(parent)
        self.reminder_id = reminder.id
        self.setWindowTitle("Reminder - Jarvis Book Writer")
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
        self.setModal(False)
        lay = QVBoxLayout(self)
        self.text = QLabel(reminder.text)
        self.text.setWordWrap(True)
        font = QFont()
        font.setPointSize(16)
        font.setBold(True)
        self.text.setFont(font)
        lay.addWidget(self.text)
        late = reminder.late_minutes(now)
        when = describe_time(reminder.due_at, now)
        self.when = QLabel(f"Reminder for {when}" + (f" - missed by {late} min while the app was closed"
                                                     if late >= 2 else ""))
        lay.addWidget(self.when)
        row = QHBoxLayout()
        self.minutes = QComboBox()
        for m in SNOOZE_MINUTES:
            self.minutes.addItem(f"{m} min", m)
        self.minutes.setCurrentIndex(SNOOZE_MINUTES.index(10))
        self.snooze_btn = QPushButton("Snooze")
        self.snooze_btn.clicked.connect(lambda: self.snooze.emit(self.reminder_id, self.minutes.currentData()))
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setDefault(True)
        self.stop_btn.clicked.connect(lambda: self.dismiss.emit(self.reminder_id))
        row.addWidget(self.minutes)
        row.addWidget(self.snooze_btn)
        row.addStretch()
        row.addWidget(self.stop_btn)
        lay.addLayout(row)
        self.resize(420, 160)
        self.answered = False

    def closeEvent(self, event) -> None:
        if not self.answered:  # closing the pop-up with X stops the alarm instead of leaving it ringing unseen
            self.answered = True
            self.dismiss.emit(self.reminder_id)
        super().closeEvent(event)


class AlarmCenter(QObject):
    """Rings reminders: pop-up on top of every window, a looping sound, a Windows notification, and a note to
    Jarvis when a voice chat is on. Planner events arrive on the reminder clock's thread."""
    _event = pyqtSignal(str, object)

    def __init__(self, planner: Planner, window: QWidget, sound_path: Path,
                 tell_jarvis: Optional[Callable[[str], None]] = None):
        super().__init__(window)
        self.planner = planner
        self.window = window
        self.tell_jarvis = tell_jarvis
        self.dialogs: dict[int, AlarmDialog] = {}
        self.sound_on = False
        self._sound = None
        self._sound_path = sound_path
        self.tray: Optional[QSystemTrayIcon] = None
        self.notified: list[str] = []
        self._limit = QTimer(self)
        self._limit.setSingleShot(True)
        self._limit.setInterval(RING_LIMIT_MS)
        self._limit.timeout.connect(self._silence)
        self._closed = False
        self._event.connect(self._on_event)
        planner.subscribe(self._from_planner)
        for r in planner.ringing():  # still ringing when the app last closed
            self._ring(r)

    def _from_planner(self, event: str, payload) -> None:
        if not self._closed and event in ("ringing", "stopped"):
            self._event.emit(event, payload)

    def _on_event(self, event: str, payload) -> None:
        if self._closed:
            return
        if event == "ringing":
            self._ring(payload)
        elif event == "stopped":
            self._close_dialog(int(payload))

    def _ring(self, reminder: Reminder) -> None:
        dlg = self.dialogs.get(reminder.id)
        if dlg is None:
            dlg = AlarmDialog(reminder, self.planner.clock(), self.window)
            dlg.snooze.connect(self.snooze)
            dlg.dismiss.connect(self.stop)
            self.dialogs[reminder.id] = dlg
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()
        QApplication.alert(self.window)
        message = f"Reminder: {reminder.text}"
        self.notified.append(message)
        self._notify(message)
        if reminder.alarm:
            self._start_sound()
        if self.tell_jarvis is not None:
            try:
                self.tell_jarvis(f"(System note: a reminder is ringing now: '{reminder.text}'. Tell the author "
                                 "out loud in one short sentence. They can say snooze or stop.)")
            except Exception:
                log.exception("could not tell Jarvis about the reminder")

    def _notify(self, message: str) -> None:
        if _quiet() or not QSystemTrayIcon.isSystemTrayAvailable():
            return
        if self.tray is None:
            icon = self.window.windowIcon()
            if icon.isNull():
                icon = self.window.style().standardIcon(QStyle.StandardPixmap.SP_MessageBoxInformation)
            self.tray = QSystemTrayIcon(icon, self)
            self.tray.setToolTip("Jarvis Book Writer")
            self.tray.messageClicked.connect(lambda: (self.window.showNormal(), self.window.raise_()))
            self.tray.show()
        self.tray.showMessage("Jarvis Book Writer", message, QSystemTrayIcon.MessageIcon.Information, 15000)

    def _start_sound(self) -> None:
        self.sound_on = True
        self._limit.start()
        if _quiet():
            return
        try:
            if self._sound is None:
                from PyQt6.QtMultimedia import QSoundEffect
                self._sound = QSoundEffect(self)
                self._sound.setSource(QUrl.fromLocalFile(str(make_alarm_sound(self._sound_path))))
                self._sound.setLoopCount(QSoundEffect.Loop.Infinite.value)
                self._sound.setVolume(0.9)
            if not self._sound.isPlaying():
                self._sound.play()
        except Exception:
            log.exception("alarm sound failed")
            QApplication.beep()

    def _silence(self) -> None:
        self.sound_on = False
        self._limit.stop()
        if self._sound is not None:
            self._sound.stop()

    def _close_dialog(self, reminder_id: int) -> None:
        dlg = self.dialogs.pop(reminder_id, None)
        if dlg is not None:
            dlg.answered = True
            dlg.close()
            dlg.deleteLater()
        if not any(r.alarm for r in self.planner.ringing()):
            self._silence()

    def snooze(self, reminder_id: int, minutes: int) -> None:
        try:
            self.planner.snooze(reminder_id, minutes)
        except Exception as exc:
            log.warning("snooze failed: %s", exc)
            self._close_dialog(reminder_id)

    def stop(self, reminder_id: int) -> None:
        try:
            self.planner.dismiss(reminder_id)
        except Exception as exc:
            log.warning("stopping the alarm failed: %s", exc)
            self._close_dialog(reminder_id)

    def shutdown(self) -> None:
        self._closed = True
        self._silence()
        for dlg in list(self.dialogs.values()):
            dlg.answered = True
            dlg.close()
        self.dialogs.clear()
        if self.tray is not None:
            self.tray.hide()


class PlannerTab(QWidget):
    _changed = pyqtSignal()

    def __init__(self, studio, parent=None):
        super().__init__(parent)
        self.studio = studio
        self.planner: Planner = studio.planner
        self._closed = False
        self._loading = False
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(150)
        self._refresh_timer.timeout.connect(self.refresh)
        self._changed.connect(self._refresh_timer.start)
        self.planner.subscribe(lambda e, p: None if self._closed else self._changed.emit())
        lay = QHBoxLayout(self)

        left = QVBoxLayout()
        self.calendar = QCalendarWidget()
        self.calendar.setGridVisible(True)
        self.calendar.selectionChanged.connect(self.refresh)
        self.calendar.currentPageChanged.connect(lambda *_: self._mark_busy_days())
        left.addWidget(self.calendar)
        today = QPushButton("Today")
        today.clicked.connect(lambda: self.calendar.setSelectedDate(QDate.currentDate()))
        left.addWidget(today)
        left.addWidget(self._schedule_box())
        left.addStretch()
        lay.addLayout(left, 3)

        mid = QVBoxLayout()
        self.day_label = QLabel()
        self.day_label.setObjectName("dayLabel")
        mid.addWidget(self.day_label)
        self.items = QListWidget()
        self.items.itemChanged.connect(self._item_checked)
        mid.addWidget(self.items, 1)
        self.show_todo = QCheckBox("Also show to-dos with no date")
        self.show_todo.setChecked(True)
        self.show_todo.toggled.connect(self.refresh)
        mid.addWidget(self.show_todo)
        add = QHBoxLayout()
        self.item_title = QLineEdit()
        self.item_title.setPlaceholderText("Add to this day, e.g. 'Edit chapter 4'")
        self.item_title.returnPressed.connect(self._add_item)
        self.item_time = QTimeEdit(QTime(9, 0))
        self.item_time.setDisplayFormat("h:mm AP")
        self.item_all_day = QCheckBox("All day")
        self.item_all_day.toggled.connect(lambda on: self.item_time.setEnabled(not on))
        self.item_remind = QCheckBox("Remind me")
        add_btn = QPushButton("Add")
        add_btn.clicked.connect(self._add_item)
        for w in (self.item_title, self.item_time, self.item_all_day, self.item_remind, add_btn):
            add.addWidget(w, 3 if w is self.item_title else 0)
        mid.addLayout(add)
        row = QHBoxLayout()
        self.item_book = QComboBox()
        row.addWidget(QLabel("Book:"))
        row.addWidget(self.item_book, 1)
        self.no_date_btn = QPushButton("Add as a to-do (no date)")
        self.no_date_btn.clicked.connect(lambda: self._add_item(dated=False))
        self.delete_btn = QPushButton("Delete selected")
        self.delete_btn.clicked.connect(self._delete_item)
        row.addWidget(self.no_date_btn)
        row.addWidget(self.delete_btn)
        mid.addLayout(row)
        lay.addLayout(mid, 4)

        right = QVBoxLayout()
        right.addWidget(QLabel("<b>Reminders</b>"))
        self.reminders = QListWidget()
        right.addWidget(self.reminders, 1)
        buttons = QHBoxLayout()
        for label, slot in (("Snooze 10 min", self._snooze), ("Stop alarm", self._stop),
                            ("Cancel reminder", self._cancel)):
            b = QPushButton(label)
            b.clicked.connect(slot)
            buttons.addWidget(b)
        right.addLayout(buttons)
        form = QFormLayout()
        self.rem_text = QLineEdit()
        self.rem_text.setPlaceholderText("What to remind you of")
        self.rem_text.returnPressed.connect(self._add_reminder)
        self.rem_when = QDateTimeEdit()
        self.rem_when.setCalendarPopup(True)
        self.rem_when.setDisplayFormat("ddd MMM d yyyy  h:mm AP")
        self.rem_repeat = QComboBox()
        for r in REPEATS:
            self.rem_repeat.addItem({"none": "Once"}.get(r, r.capitalize()), r)
        self.rem_alarm = QCheckBox("Ring the alarm sound")
        self.rem_alarm.setChecked(True)
        set_btn = QPushButton("Set reminder")
        set_btn.clicked.connect(self._add_reminder)
        form.addRow("Remind me to", self.rem_text)
        form.addRow("When", self.rem_when)
        form.addRow("Repeat", self.rem_repeat)
        form.addRow("", self.rem_alarm)
        form.addRow("", set_btn)
        right.addLayout(form)
        self.status = QLabel()
        self.status.setWordWrap(True)
        right.addWidget(self.status)
        lay.addLayout(right, 4)
        self._reset_reminder_time()
        self.refresh_books()
        self.refresh()

    def _schedule_box(self) -> QGroupBox:
        box = QGroupBox("Writing schedule")
        form = QFormLayout(box)
        self.sched_book = QComboBox()
        self.sched_per_day = QSpinBox()
        self.sched_per_day.setRange(1, 50)
        self.sched_days = QSpinBox()
        self.sched_days.setRange(0, 366)
        self.sched_days.setSpecialValueText("Until the book is done")
        self.sched_time = QTimeEdit(QTime(9, 0))
        self.sched_time.setDisplayFormat("h:mm AP")
        self.sched_weekdays = QCheckBox("Weekdays only")
        self.sched_remind = QCheckBox("Remind me each session")
        self.sched_remind.setChecked(True)
        make = QPushButton("Make schedule (starts on the selected day)")
        make.clicked.connect(self._make_schedule)
        for label, w in (("Book", self.sched_book), ("Chapters a day", self.sched_per_day),
                         ("Days", self.sched_days), ("Time", self.sched_time), ("", self.sched_weekdays),
                         ("", self.sched_remind), ("", make)):
            form.addRow(label, w)
        return box

    # ---- data -> widgets ------------------------------------------------------------------------
    def selected_day(self) -> date:
        return self.calendar.selectedDate().toPyDate()

    def refresh_books(self) -> None:
        books = self.studio.library.list_books()
        for combo, blank in ((self.item_book, "(none)"), (self.sched_book, None)):
            current = combo.currentData()
            combo.blockSignals(True)
            combo.clear()
            if blank:
                combo.addItem(blank, "")
            for b in books:
                combo.addItem(b.title, b.id)
            i = combo.findData(current)
            combo.setCurrentIndex(max(0, i))
            combo.blockSignals(False)

    def refresh(self) -> None:
        if self._closed:
            return
        day = self.selected_day()
        now = self.planner.clock()
        self.day_label.setText(f"<h3>{day.strftime('%A, %B %d, %Y').replace(' 0', ' ')}</h3>")
        self._loading = True
        self.items.clear()
        start, end = day_bounds(day)
        found = self.planner.items(start, end, undated=self.show_todo.isChecked())
        if not found:
            hint = QListWidgetItem("Nothing planned for this day.")
            hint.setFlags(Qt.ItemFlag.NoItemFlags)
            self.items.addItem(hint)
        for item in found:
            when = ("To-do" if item.due_at is None else "All day" if item.all_day
                    else datetime.fromtimestamp(item.due_at).strftime("%I:%M %p").lstrip("0"))
            w = QListWidgetItem(f"{when}  -  {item.title}" + (f"   ({item.notes})" if item.notes else ""))
            w.setFlags(w.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            w.setCheckState(Qt.CheckState.Checked if item.done else Qt.CheckState.Unchecked)
            w.setData(Qt.ItemDataRole.UserRole, item.id)
            if item.done:
                f = w.font()
                f.setStrikeOut(True)
                w.setFont(f)
            self.items.addItem(w)
        self._loading = False
        self.reminders.clear()
        for r in self.planner.reminders():
            label = f"{'RINGING  ' if r.status == 'ringing' else ''}{describe_time(r.due_at, now)}  -  {r.text}"
            if r.repeat != "none":
                label += f"  ({r.repeat})"
            if not r.alarm:
                label += "  (quiet)"
            w = QListWidgetItem(label)
            w.setData(Qt.ItemDataRole.UserRole, r.id)
            self.reminders.addItem(w)
        self._mark_busy_days()

    def _mark_busy_days(self) -> None:
        y, m = self.calendar.yearShown(), self.calendar.monthShown()
        first = date(y, m, 1) - timedelta(days=7)
        last = (date(y, m, 28) + timedelta(days=14))
        self.calendar.setDateTextFormat(QDate(), QTextCharFormat())  # clear the old marks
        bold = QTextCharFormat()
        bold.setFontWeight(QFont.Weight.Bold)
        bold.setForeground(Qt.GlobalColor.darkBlue)
        for d in self.planner.busy_days(*day_bounds(first)[:1], day_bounds(last)[1]):
            self.calendar.setDateTextFormat(QDate(d.year, d.month, d.day), bold)

    def _reset_reminder_time(self) -> None:
        soon = datetime.now().replace(second=0, microsecond=0) + timedelta(hours=1)
        self.rem_when.setDateTime(QDateTime(QDate(soon.year, soon.month, soon.day), QTime(soon.hour, 0)))

    # ---- actions ----------------------------------------------------------------------------------
    def _result(self, out: dict, ok: str) -> bool:
        if out.get("error"):
            self.status.setText(f"<span style='color:#b00020'>{out['error']}</span>")
            return False
        self.status.setText(ok)
        return True

    def _add_item(self, dated: bool = True) -> None:
        title = self.item_title.text().strip()
        if not title:
            self.item_title.setFocus()
            return
        day = self.selected_day()
        when = ""
        if dated:
            when = day.isoformat() if self.item_all_day.isChecked() else \
                f"{day.isoformat()}T{self.item_time.time().toString('HH:mm')}"
        args = {"title": title, "when": when, "remind": bool(dated and self.item_remind.isChecked())}
        if self.item_book.currentData():
            args["book"] = self.item_book.currentText()
        out = self.studio.tools.run("add_to_planner", args)
        if self._result(out, f"Added: {title}" + (" (with a reminder)" if out.get("reminder") else "")):
            self.item_title.clear()
            if out.get("reminder_error"):
                self.status.setText(f"Added, but no reminder: {out['reminder_error']}")
        self.refresh()

    def _item_checked(self, w: QListWidgetItem) -> None:
        item_id = w.data(Qt.ItemDataRole.UserRole)
        if self._loading or item_id is None:
            return
        self.studio.tools.run("update_planner_item", {"item_id": item_id,
                                                      "done": w.checkState() == Qt.CheckState.Checked})
        self._refresh_timer.start()

    def _selected(self, lst: QListWidget) -> Optional[int]:
        w = lst.currentItem()
        return w.data(Qt.ItemDataRole.UserRole) if w is not None else None

    def _delete_item(self) -> None:
        item_id = self._selected(self.items)
        if item_id is None:
            return
        self._result(self.studio.tools.run("delete_planner_item", {"item_id": item_id}), "Deleted.")
        self.refresh()

    def _add_reminder(self) -> None:
        text = self.rem_text.text().strip()
        if not text:
            self.rem_text.setFocus()
            return
        when = self.rem_when.dateTime().toPyDateTime().replace(second=0, microsecond=0).isoformat(timespec="minutes")
        out = self.studio.tools.run("set_reminder", {"text": text, "when": when,
                                                     "repeat": self.rem_repeat.currentData(),
                                                     "alarm": self.rem_alarm.isChecked()})
        if self._result(out, f"Reminder set for {out.get('set', {}).get('when', '')}: {text}"):
            self.rem_text.clear()
            self._reset_reminder_time()
        self.refresh()

    def _reminder_action(self, tool: str, ok: str, **extra) -> None:
        rid = self._selected(self.reminders)
        args = {"reminder_id": rid, **extra} if rid is not None else dict(extra)
        if tool == "cancel_reminder" and rid is None:
            return
        self._result(self.studio.tools.run(tool, args), ok)
        self.refresh()

    def _snooze(self) -> None:
        self._reminder_action("snooze_reminder", "Snoozed for 10 minutes.", minutes=10)

    def _stop(self) -> None:
        self._reminder_action("stop_alarm", "Alarm stopped.")

    def _cancel(self) -> None:
        self._reminder_action("cancel_reminder", "Reminder cancelled.")

    def _make_schedule(self) -> None:
        if not self.sched_book.currentData():
            QMessageBox.information(self, "Writing schedule", "Create a book first.")
            return
        out = self.studio.tools.run("plan_writing_schedule", {
            "book": self.sched_book.currentText(), "start": self.selected_day().isoformat(),
            "time": self.sched_time.time().toString("h:mm AP"), "chapters_per_day": self.sched_per_day.value(),
            "days": self.sched_days.value(), "remind": self.sched_remind.isChecked(),
            "weekdays_only": self.sched_weekdays.isChecked()})
        if not out.get("error"):
            self._result(out, f"Planned {out['sessions']} writing sessions: {out['first']['title']} "
                              f"({out['first']['when']}) to {out['last']['title']} ({out['last']['when']}).")
        else:
            self._result(out, "")
        self.refresh()

    def shutdown(self) -> None:
        self._closed = True
        self._refresh_timer.stop()
