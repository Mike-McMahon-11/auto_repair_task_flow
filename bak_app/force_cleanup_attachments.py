from app import app, db, TaskAttachment, Task, TaskComment, TaskRead

with app.app_context():
    print("Starting forced cleanup of orphan attachments...")

    # 1. Delete ALL attachments that have no valid task (orphans)
    orphans = TaskAttachment.query.filter(
        ~TaskAttachment.task_id.in_(db.session.query(Task.id))
    ).all()

    print(f"Found {len(orphans)} orphan attachments.")

    for att in orphans:
        print(f"→ Deleting orphan attachment ID {att.id} (filename: {att.filename})")
        db.session.delete(att)

    db.session.commit()
    print("Orphan attachments deleted.")

    # 2. Also clean any attachments where task_id IS NULL (should never happen)
    null_attachments = TaskAttachment.query.filter(TaskAttachment.task_id.is_(None)).all()
    if null_attachments:
        print(f"Found {len(null_attachments)} attachments with NULL task_id - deleting them.")
        for att in null_attachments:
            db.session.delete(att)
        db.session.commit()

    # 3. Optional: Delete any other broken child records
    print("Cleaning TaskRead and TaskComment for any deleted tasks...")
    TaskRead.query.filter(
        ~TaskRead.task_id.in_(db.session.query(Task.id))
    ).delete(synchronize_session=False)

    TaskComment.query.filter(
        ~TaskComment.task_id.in_(db.session.query(Task.id))
    ).delete(synchronize_session=False)

    db.session.commit()

    print("✅ Cleanup completed successfully.")
    print("You can now re-enable the purge in app.py and start the app.")