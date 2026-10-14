"""Team-side actions on help requests."""
from . import messages as M

TRANSITIONS = {
    "waiting": {"accepted", "closed"},
    "accepted": {"replied", "closed"},
    "replied": {"accepted", "closed"},
    "closed": set(),
}


def set_help_status(db, help_id, new_status):
    """Returns (ok, info). Same status again is a harmless no-op (double click)."""
    with db.lock:
        h = db.get_help(help_id)
        if h is None:
            return False, "not_found"
        if new_status not in TRANSITIONS:
            return False, "invalid_status"
        if new_status == h["status"]:
            return True, "unchanged"
        if new_status not in TRANSITIONS[h["status"]]:
            return False, f"cannot_go_from_{h['status']}_to_{new_status}"
        db.update_help(help_id, status=new_status)
        db.add_outbox(h["user_id"], [M.help_status_changed(help_id, new_status)])
        return True, "changed"
