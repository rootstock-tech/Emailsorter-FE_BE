"""Gmail label helpers: look up, create, and apply labels."""

# Cache of label name -> label id to avoid repeat labels().list() API calls
# within a single run.
_label_cache = {}


def get_or_create_label(service, label_name, hidden=False):
    """Return the label id for ``label_name``, creating the label if needed.

    Results are cached in a module-level dict so repeated calls for the same
    label name within a run do not hit the Gmail API again. When ``hidden`` is
    True the label is kept internal -- not shown in Gmail's label list or on
    messages (used for the bookkeeping "AI-Processed" label). An existing label
    that was created visible is patched to hidden.
    """
    if label_name in _label_cache:
        return _label_cache[label_name]

    list_visibility = "labelHide" if hidden else "labelShow"
    message_visibility = "hide" if hidden else "show"

    response = service.users().labels().list(userId="me").execute()
    for label in response.get("labels", []):
        if label.get("name") == label_name:
            label_id = label["id"]
            # If it should be internal but was created visible earlier, hide it.
            if hidden and (
                label.get("labelListVisibility") != "labelHide"
                or label.get("messageListVisibility") != "hide"
            ):
                try:
                    service.users().labels().patch(
                        userId="me",
                        id=label_id,
                        body={
                            "labelListVisibility": "labelHide",
                            "messageListVisibility": "hide",
                        },
                    ).execute()
                except Exception:  # noqa: BLE001 - visibility tweak is best-effort
                    pass
            _label_cache[label_name] = label_id
            return label_id

    created = (
        service.users()
        .labels()
        .create(
            userId="me",
            body={
                "name": label_name,
                "labelListVisibility": list_visibility,
                "messageListVisibility": message_visibility,
            },
        )
        .execute()
    )
    _label_cache[label_name] = created["id"]
    return created["id"]


def apply_label(service, message_id, label_id):
    """Add a label to a message via users().messages().modify()."""
    return (
        service.users()
        .messages()
        .modify(
            userId="me",
            id=message_id,
            body={"addLabelIds": [label_id]},
        )
        .execute()
    )
