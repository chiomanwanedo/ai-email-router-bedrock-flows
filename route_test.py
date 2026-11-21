"""Send every test email through the live alias of the Ashgrove email router and report, for each one:
the label from the classifier, the route the condition node chose, whether it matches the expected route, the tokens used and the cost.
Run in AWS CloudShell (Europe (London) eu-west-2):  python3 route_test.py"""
import csv, json, os
import boto3

REGION = "eu-west-2"
FLOW_NAME = os.environ.get("FLOW_NAME", "ashgrove-email-router")
ALIAS_NAME = os.environ.get("ALIAS_NAME", "live")
# US dollars per 1 million tokens, Europe (London), Standard tier (AWS Price List, 14 Oct 2026)
PRICES = {"amazon.nova-micro-v1:0": (0.049, 0.196),
          "amazon.nova-lite-v1:0": (0.084, 0.336),
          "amazon.nova-pro-v1:0": (1.13, 4.52)}
NODE_PRICE = 0.035 / 1000            # Bedrock Flows: US dollars per node transition
ROUTES = {"isRepair": "repair", "isViewing": "viewing", "isRent": "rent", "default": "escalate"}   # condition -> route

agent = boto3.client("bedrock-agent", region_name=REGION)
runtime = boto3.client("bedrock-agent-runtime", region_name=REGION)

# 1. Find the flow and its alias by name
flow_id = next(f["id"] for f in agent.list_flows()["flowSummaries"] if f["name"] == FLOW_NAME)
alias_id = next(a["id"] for a in agent.list_flow_aliases(flowIdentifier=flow_id)["flowAliasSummaries"]
                if a["name"] == ALIAS_NAME)
version = agent.get_flow_alias(flowIdentifier=flow_id, aliasIdentifier=alias_id)["routingConfiguration"][0]["flowVersion"]
nodes_def = agent.get_flow_version(flowIdentifier=flow_id, flowVersion=version)["definition"]["nodes"]
input_node = next(n["name"] for n in nodes_def if n["type"] == "Input")
models = {}                          # prompt version ARN -> model ID, looked up once


def model_of(prompt_arn):
    if prompt_arn not in models:
        pid, ver = prompt_arn.split("/")[-1].split(":")
        p = agent.get_prompt(promptIdentifier=pid, promptVersion=ver)
        models[prompt_arn] = p["variants"][0]["modelId"]
    return models[prompt_arn]


def run(email):
    """2. Invoke the flow with trace on; collect the output node, the label and the tokens of every prompt node."""
    resp = runtime.invoke_flow(flowIdentifier=flow_id, flowAliasIdentifier=alias_id, enableTrace=True,
                               inputs=[{"nodeName": input_node, "nodeOutputName": "document",
                                        "content": {"document": email}}])
    route, reply, label, nodes, calls = "none", "", "", 1, []
    for event in resp["responseStream"]:
        if "flowOutputEvent" in event:
            reply = event["flowOutputEvent"]["content"]["document"]
        trace = event.get("flowTraceEvent", {}).get("trace", {})
        for c in trace.get("conditionNodeResultTrace", {}).get("satisfiedConditions", []):
            route = ROUTES.get(c["conditionName"], c["conditionName"])
        if "nodeInputTrace" in trace:
            nodes += 1
        if trace.get("nodeOutputTrace", {}).get("nodeName") == "EmailClassifier":
            label = trace["nodeOutputTrace"]["fields"][0]["content"]["document"]
        if "nodeActionTrace" in trace:
            act = trace["nodeActionTrace"]
            usage = act["operationResponse"]["usage"]
            calls.append((model_of(act["operationRequest"]["modelId"]), usage["inputTokens"], usage["outputTokens"]))
    return route, reply, label, nodes, calls


def cost(calls, model=None):
    return sum(i * PRICES[model or m][0] / 1e6 + o * PRICES[model or m][1] / 1e6 for m, i, o in calls)


rows = []
with open("test-emails.jsonl", encoding="utf-8") as f:
    emails = [json.loads(line) for line in f if line.strip()]
print(f"{'id':<4} {'expected':<9} {'label':<9} {'route':<9} {'ok':<4} {'in':>4} {'out':>4} {'cost $':>9}")
for e in emails:
    route, reply, label, nodes, calls = run(e["email"])
    ok = route == e["expected"]
    tin, tout = sum(c[1] for c in calls), sum(c[2] for c in calls)
    total = cost(calls) + nodes * NODE_PRICE
    rows.append({"id": e["id"], "expected": e["expected"], "label": label, "route": route, "ok": ok,
                 "input_tokens": tin, "output_tokens": tout, "nodes": nodes, "cost_usd": round(total, 6),
                 "all_pro_cost_usd": round(cost(calls, "amazon.nova-pro-v1:0") + nodes * NODE_PRICE, 6),
                 "reply": reply})
    print(f"{e['id']:<4} {e['expected']:<9} {label[:9]:<9} {route:<9} {'ok' if ok else 'MISS':<4} "
          f"{tin:>4} {tout:>4} {total:>9.6f}")

# 3. Summary: accuracy, cost per email and per 1,000 emails, and the same work on Nova Pro for comparison
n = len(rows)
avg = sum(r["cost_usd"] for r in rows) / n
pro = sum(r["all_pro_cost_usd"] for r in rows) / n
print(f"\nCorrect routes: {sum(r['ok'] for r in rows)}/{n}")
print(f"Average cost per email: ${avg:.6f}  (${1000 * avg:.2f} per 1,000 emails)")
print(f"Same emails with Nova Pro in every prompt: ${1000 * pro:.2f} per 1,000 emails")
with open("route-results.csv", "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=list(rows[0]))
    w.writeheader(); w.writerows(rows)
print("Wrote route-results.csv")
