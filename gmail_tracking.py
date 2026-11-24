"""Read-only, least-data Gmail confirmation correlation.

Gmail access is opt-in: construct GmailReadOnlyMailbox with a caller-provided
short-lived OAuth access token. Tokens and message content are never persisted.
"""
import json
import urllib.parse
import urllib.request
from urllib.parse import urlsplit


class GmailReadOnlyMailbox:
    def __init__(self, access_token):
        if not access_token or "\n" in access_token:
            raise ValueError("authorized Gmail OAuth access token required")
        self._token = access_token

    def search(self, query, limit=100):
        if not 1 <= limit <= 500:
            raise ValueError("Gmail result limit must be between 1 and 500")
        url = "https://gmail.googleapis.com/gmail/v1/users/me/messages?" + urllib.parse.urlencode({"q": query, "maxResults": limit})
        rows = self._get(url).get("messages", [])
        result = []
        for row in rows[:limit]:
            msg = self._get("https://gmail.googleapis.com/gmail/v1/users/me/messages/" + urllib.parse.quote(row["id"]) + "?" + urllib.parse.urlencode([("format", "metadata"), ("metadataHeaders", "Subject"), ("metadataHeaders", "From"), ("metadataHeaders", "Date")]))
            headers = {h.get("name", "").lower(): h.get("value", "") for h in msg.get("payload", {}).get("headers", [])}
            result.append({"id": msg.get("id", row["id"]), "thread_id": msg.get("threadId"), "subject": headers.get("subject", ""), "from": headers.get("from", ""), "date": headers.get("date", ""), "snippet": msg.get("snippet", "")[:500]})
        return result

    def _get(self, url):
        req = urllib.request.Request(url, headers={"Authorization": "Bearer " + self._token, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as response:
            return json.loads(response.read(2 * 1024 * 1024 + 1))


def correlate_confirmations(mailbox, applications, *, limit=100):
    """Return probable confirmation matches; ambiguous mail stays for review."""
    messages = mailbox.search('newer_than:30d {subject:application subject:received subject:confirmation}', limit=limit)
    matches, review = [], []
    for message in messages:
        searchable = (message.get("subject", "") + " " + message.get("snippet", "")).casefold()
        candidates = []
        for app in applications:
            host = (urlsplit(app["url"]).hostname or "").casefold()
            employer = app.get("employer", "").casefold()
            if host and (host in searchable or (employer and employer in searchable)):
                candidates.append(app)
        if len(candidates) == 1:
            evidence = "gmail-message:" + str(message.get("id", "unknown"))
            matches.append({"application_url": candidates[0]["url"], "employer": candidates[0].get("employer"), "message_id": message.get("id"), "subject": message.get("subject", ""), "date": message.get("date", ""), "evidence": evidence})
        elif candidates:
            review.append({"message_id": message.get("id"), "reason": "ambiguous_employer_match"})
    return {"matches": matches, "review": review, "messages_scanned": len(messages)}


def sync_confirmations(mailbox, ledger, *, limit=100):
    rows = ledger.rows()
    result = correlate_confirmations(mailbox, rows, limit=limit)
    updated = []
    for match in result["matches"]:
        row = next((r for r in rows if r["url"] == match["application_url"]), None)
        if row and row["state"] in ("review", "prepared"):
            if ledger.transition(row["url"], row["state"], "confirmed", evidence=match["evidence"]):
                updated.append(match["application_url"])
    result["updated"] = updated
    result["daily_summary"] = {"confirmations": len(updated), "manual_review": len(result["review"]), "external_message_sent": False}
    return result
