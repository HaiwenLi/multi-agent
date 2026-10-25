"""Seat-bound native boundary for deterministic fixtures and future live preflight."""
import time

from native_supervisor import native_argv
from run_store import Blocked


class NativeAdapter:
    def __init__(self, supervisor, capture):
        self.supervisor = supervisor
        self.capture = capture
        self.sessions = {}
        self.busy = set()

    def invoke(self, seat: str, session: str, brief: str, cwd) -> dict:
        if seat in self.busy:
            raise Blocked('seat_busy')
        bound = self.sessions.get(seat)
        if bound and bound != session:
            raise Blocked('seat_session_mismatch')
        if any(value == session and key != seat for key, value in self.sessions.items()):
            raise Blocked('session_already_bound')
        argv = native_argv(seat, session, brief)
        self.sessions[seat] = session
        self.busy.add(seat)
        self.supervisor.launch(seat, argv, cwd)
        tool_names, denied, final_text, usage = [], False, None, False
        for event in self.capture(seat, session, brief, cwd):
            if event.get('type') == 'tool':
                name = event.get('name')
                if isinstance(name, str):
                    tool_names.append(name)
                denied = denied or event.get('denied') is True
            elif event.get('type') == 'final' and isinstance(event.get('text'), str):
                text = event['text']
                if 'authorization:' in text.lower():
                    raise Blocked('unfiltered_native_output')
                final_text = text
            elif event.get('type') == 'usage':
                usage = True
        if final_text is None:
            raise Blocked('missing_filtered_final')
        return {'seat': seat, 'session': session, 'tool_names': tool_names, 'denied': denied,
                'final_text': final_text, 'usage_available': usage, 'exit': 'captured'}

    def complete(self, seat: str):
        self.busy.discard(seat)

    def stop(self, seat: str) -> dict:
        started = time.monotonic()
        evidence = self.supervisor.request_stop(seat, 'operator')
        self.busy.discard(seat)
        active = getattr(evidence, 'active', None)
        stable = getattr(evidence, 'stable', False)
        report = {
            'seat': seat, 'freeze_seconds': getattr(evidence, 'freeze_seconds', time.monotonic() - started),
            'active': active, 'children_stopped': stable, 'services_stopped': False,
            'permission_verified': False,
        }
        report['verified'] = (report['freeze_seconds'] <= 1 and active == 0 and stable
                              and report['services_stopped'] and report['permission_verified'])
        report['status'] = 'verified' if report['verified'] else 'partial'
        return report
