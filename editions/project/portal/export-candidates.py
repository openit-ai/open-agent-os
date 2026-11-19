#!/usr/bin/env python3
"""Approved portal applicants as Company member candidates (ADR 0002 §2.4).

    python3 -I export-candidates.py PORTAL_DB   (also read from stdin by project-to-company.sh over ssh)

Prints JSON: each approved request with its Mattermost user ID once known, as a member
candidate in state "invited" and role "member". The Company G10 mapping step stays with the
administrator: candidates are proposals, never active members.
"""
import json
import sqlite3
import sys
import time


def main(argv):
    if len(argv) != 2:
        sys.stderr.write("usage: export-candidates.py PORTAL_DB\n")
        return 2
    db = sqlite3.connect("file:%s?mode=ro" % argv[1], uri=True)
    db.row_factory = sqlite3.Row
    rows = db.execute("SELECT r.name, r.email, r.org, r.mattermost_user_id, r.decided_at, d.decider_id"
                      " FROM requests r LEFT JOIN decisions d ON d.request_id = r.id AND d.decision = 'approved'"
                      " WHERE r.state = 'approved' AND r.email IS NOT NULL ORDER BY r.decided_at").fetchall()
    candidates = [{"name": row["name"], "email": row["email"], "org": row["org"] or "",
                   "project_user_id": row["mattermost_user_id"] or "", "role": "member", "state": "invited",
                   "approved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(row["decided_at"] or 0)),
                   "approved_by": row["decider_id"] or ""} for row in rows]
    json.dump({"source": "oaos-project-portal", "schema": 1,
               "exported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "candidates": candidates},
              sys.stdout, ensure_ascii=False, indent=1)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
