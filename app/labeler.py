"""Gmail label helpers: look up, create, and apply labels."""

# Cache of label name -> label id to avoid repeat labels().list() API calls
# within a single run.
_label_cache = {}


def get_or_create_label(service, label_name):
    """Return the label id for ``label_name``, creating the label if needed.

    Results are cached in a module-level dict so repeated calls for the same
    label name within a run do not hit the Gmail API again.
    """
    if label_name in _label_cache:
        return _label_cache[label_name]

    response = service.users().labels().list(userId="me").execute()
    for label in response.get("labels", []):
        if label.get("name") == label_name:
            _label_cache[label_name] = label["id"]
            return label["id"]

    created = (
        service.users()
        .labels()
        .create(
            userId="me",
            body={
                "name": label_name,
                "labelListVisibility": "labelShow",
                "messageListVisibility": "show",
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
