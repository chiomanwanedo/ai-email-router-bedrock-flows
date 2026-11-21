"""Contact Ashgrove: the Lambda function behind the web page.

The page sends POST {"question": "..."} with an x-passcode header. This function:
  1. checks the passcode (stored in the PASSCODE environment variable, never in the page);
  2. refuses empty questions and questions over MAX_CHARS characters (cost guard);
  3. calls ask(question), the part that uses what you built earlier in the lab;
  4. returns {"answer": "...", "sources": [{"name": "...", "excerpt": "..."}], "blocked": true|false}.
API Gateway adds the CORS headers, so only your CloudFront page can call it from a browser.
"""
import base64
import hmac
import json
import os

import boto3

PASSCODE = os.environ["PASSCODE"]
MAX_CHARS = int(os.environ.get("MAX_CHARS", "500"))

# ---- LAB-SPECIFIC: start ----
FLOW_ID = os.environ["FLOW_ID"]
ALIAS_ID = os.environ["ALIAS_ID"]
ROUTES = {"RepairOutput": "repair", "ViewingOutput": "viewing", "RentOutput": "rent", "EscalateOutput": "escalate"}
TEAMS = {"repair": "Repairs", "viewing": "Viewings and lettings", "rent": "Rent and deposit", "escalate": "Needs a person now"}
runtime = boto3.client("bedrock-agent-runtime")


def ask(question):
    """Send the message through the live alias of the Ashgrove email router (Task 4) and return the route and the text."""
    r = runtime.invoke_flow(flowIdentifier=FLOW_ID, flowAliasIdentifier=ALIAS_ID,
                            inputs=[{"nodeName": "FlowInputNode", "nodeOutputName": "document",
                                     "content": {"document": question}}])
    route, text = "escalate", ""
    for event in r["responseStream"]:
        if "flowOutputEvent" in event:
            out = event["flowOutputEvent"]
            route, text = ROUTES.get(out["nodeName"], "escalate"), str(out["content"]["document"]).strip()
    if not text:
        text = "No note was written. A person should read this message."
    kind = "Handover note for the duty manager" if route == "escalate" else "Draft reply, ready to edit"
    return {"answer": text, "route": route, "blocked": route == "escalate",
            "sources": [{"name": "Routed to: " + TEAMS[route], "excerpt": kind}]}
# ---- LAB-SPECIFIC: end ----


def reply(status, body):
    return {"statusCode": status, "headers": {"content-type": "application/json"}, "body": json.dumps(body)}


def lambda_handler(event, context):
    # The quick-create $default route also receives the browser's CORS preflight (OPTIONS, no passcode).
    # Answer it empty: API Gateway adds the CORS headers from the API's CORS settings.
    if event.get("requestContext", {}).get("http", {}).get("method") == "OPTIONS":
        return {"statusCode": 204}
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    if not hmac.compare_digest(headers.get("x-passcode", "").encode(), PASSCODE.encode()):
        return reply(401, {"error": "Wrong passcode."})
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8", "replace")
    try:
        question = str(json.loads(raw).get("question", "")).strip()
    except (ValueError, AttributeError):
        return reply(400, {"error": "Send JSON like {\"question\": \"...\"}."})
    if not question:
        return reply(400, {"error": "Type a question first."})
    if len(question) > MAX_CHARS:
        return reply(400, {"error": f"Keep the question under {MAX_CHARS} characters."})
    try:
        return reply(200, ask(question))
    except Exception as e:  # the log keeps the detail; the page gets a short message
        print("ERROR", repr(e))
        return reply(502, {"error": "The assistant could not answer just now. Try again in a minute."})
