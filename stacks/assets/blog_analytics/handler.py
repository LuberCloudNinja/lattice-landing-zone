"""blog_analytics ingest handler -- POST /events from the blog's client-side
AnalyticsBeacon (app/frontend-next/components/AnalyticsBeacon.tsx).

Reached through CloudFront at /analytics/* (same-origin, no CORS needed --
see threetier_stack.py). Records one raw event per call into DynamoDB (TTL'd
-- see blog_analytics_stack.py for retention) and publishes aggregate
CloudWatch metrics so the dashboard stays useful long after any individual
raw event has expired.

Privacy note (see README.md's Blog Analytics section): source IP and country
are captured because the project owner asked for per-visitor geographic and
technical analytics on their own public blog. IP addresses are personal data
under GDPR/CCPA -- this handler stores them only in the TTL'd raw-events
table (not the permanent CloudWatch aggregates), and the site should carry a
short privacy notice if it draws EU/UK traffic. Not this handler's job to
enforce that; flagging it here so it isn't missed.
"""

import json
import os
import time
import uuid

import boto3

dynamodb = boto3.resource("dynamodb")
cloudwatch = boto3.client("cloudwatch")

TABLE_NAME = os.environ["TABLE_NAME"]
METRICS_NAMESPACE = os.environ.get("METRICS_NAMESPACE", "LatticeLab/BlogAnalytics")
TTL_DAYS = int(os.environ.get("TTL_DAYS", "180"))

table = dynamodb.Table(TABLE_NAME)

ALLOWED_EVENT_TYPES = {"page_view", "read_complete", "github_click", "session_end"}


def _client_ip(headers: dict) -> str:
    # CloudFront sets X-Forwarded-For to "<client-ip>, <cloudfront-edge-ip>,
    # ..."; the first entry is the real viewer, not something the client can
    # spoof since CloudFront overwrites this header itself.
    xff = headers.get("x-forwarded-for") or headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()
    return headers.get("x-real-ip", "unknown")


def _cors_headers() -> dict:
    return {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
    }


def handler(event, context):
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    method = event.get("requestContext", {}).get("http", {}).get("method", "POST")

    if method == "OPTIONS":
        return {"statusCode": 204, "headers": _cors_headers()}

    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return {"statusCode": 400, "headers": _cors_headers(), "body": "invalid json"}

    event_type = body.get("type")
    if event_type not in ALLOWED_EVENT_TYPES:
        return {"statusCode": 400, "headers": _cors_headers(), "body": "unknown event type"}

    now_ms = int(time.time() * 1000)
    country = headers.get("cloudfront-viewer-country", "unknown")
    source_ip = _client_ip(headers)
    session_id = str(body.get("sessionId") or uuid.uuid4())

    item = {
        "sessionId": session_id,
        "eventTimestamp": now_ms,
        "eventId": str(uuid.uuid4()),
        "eventType": event_type,
        "path": str(body.get("path", ""))[:512],
        "country": country,
        "sourceIp": source_ip,
        "userAgent": headers.get("user-agent", "unknown")[:512],
        "referrer": headers.get("referer", "")[:512],
        "expiresAt": int(time.time()) + TTL_DAYS * 86400,
    }
    if event_type == "session_end":
        item["timeOnPageMs"] = int(body.get("timeOnPageMs", 0))
        item["maxScrollPercent"] = int(body.get("maxScrollPercent", 0))
        item["completedRead"] = bool(body.get("completedRead", False))

    table.put_item(Item=item)

    metric_data = [{
        "MetricName": {
            "page_view": "PageViews",
            "read_complete": "ReadCompletions",
            "github_click": "GithubClicks",
            "session_end": "SessionEnds",
        }[event_type],
        "Value": 1,
        "Unit": "Count",
        "Dimensions": [{"Name": "Country", "Value": country}],
    }]
    if event_type == "session_end" and item["timeOnPageMs"] > 0:
        metric_data.append({
            "MetricName": "TimeOnPageSeconds",
            "Value": item["timeOnPageMs"] / 1000.0,
            "Unit": "Seconds",
        })
        metric_data.append({
            "MetricName": "MaxScrollPercent",
            "Value": item["maxScrollPercent"],
            "Unit": "Percent",
        })

    cloudwatch.put_metric_data(Namespace=METRICS_NAMESPACE, MetricData=metric_data)

    return {"statusCode": 204, "headers": _cors_headers()}
