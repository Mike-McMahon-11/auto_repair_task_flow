# purge_denied_vendors.py
"""
One-time cleanup for vendor approvals with status LIKE 'denied%'.
- Reflects the DB to find *all* tables that FK to task.id
- Deletes child rows first, then deletes the tasks
- Prints what it did, and verifies the final count
"""

from sqlalchemy import MetaData, inspect, text
from sqlalchemy.sql import and_
from contextlib import contextmanager

from app import app, db, Task  # adjust imports if your app layout differs


@contextmanager
def appctx():
    with app.app_context():
        yield


def fk_tables_pointing_to_task(metadata):
    """Return list of (table, column) that FK to task.id."""
    out = []
    for t in metadata.tables.values():
        for c in t.columns:
            for fk in c.foreign_keys:
                ref_col = fk.column
                if ref_col.table.name.lower() == 'task' and ref_col.name.lower() == 'id':
                    out.append((t, c))
    return out


def main():
    print("Starting purge of denied vendor approvals...")

    # Enforce FKs for SQLite connections
    db.session.execute(text('PRAGMA foreign_keys = ON'))

    # Collect target task ids
    target_ids = [
        r.id for r in db.session.query(Task.id)
        .filter(Task.kind == 'vendor', Task.status.ilike('denied%'))
        .all()
    ]
    print(f"Found {len(target_ids)} denied vendor tasks -> {target_ids}")
    if not target_ids:
        print("Nothing to do.")
        return

    # Reflect DB and find all FK tables referencing task.id
    metadata = MetaData()
    metadata.reflect(bind=db.engine)
    fk_refs = fk_tables_pointing_to_task(metadata)
    if not fk_refs:
        print("Warning: no FK tables referencing task.id were discovered by reflection.")

    # 1) Delete children first, table by table (covers all known/unknown child tables)
    for table, col in fk_refs:
        q = table.delete().where(col.in_(target_ids))
        res = db.session.execute(q)
        print(f"Deleted {res.rowcount or 0} from {table.name} where {col.name} IN (targets)")
    db.session.commit()

    # 2) Delete the tasks themselves (one bulk statement)
    deleted = db.session.query(Task).filter(Task.id.in_(target_ids)).delete(synchronize_session=False)
    db.session.commit()
    print(f"Deleted {deleted} tasks from task table")

    # 3) Verify
    remaining = db.session.query(Task).filter(Task.kind=='vendor', Task.status.ilike('denied%')).count()
    print(f"Remaining denied vendor tasks: {remaining}")
    if remaining == 0:
        print("✅ Purge complete. Denied approvals are gone.")
    else:
        print("⚠️ Some tasks remain. Run again or share the output above so we can target any stragglers.")


if __name__ == "__main__":
    with appctx():
        main()
