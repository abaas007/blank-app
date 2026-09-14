"""
RentFlow RC8 Stripe Webhook Service

Run separately from Streamlit.

Render start command:
    uvicorn stripe_webhook_RC8:app --host 0.0.0.0 --port $PORT

Local/Codespaces example:
    uvicorn stripe_webhook_RC8:app --host 0.0.0.0 --port 8001

Stripe webhook destination:
    POST /stripe/webhook

Health check:
    GET /health

Configuration:
    On Render, use environment variables.
    In Codespaces/local development, .streamlit/secrets.toml is supported as a fallback.

Required for startup:
    SUPABASE_URL
    SUPABASE_SERVICE_ROLE_KEY
    STRIPE_SECRET_KEY

Required before Stripe can deliver signed webhook events:
    STRIPE_WEBHOOK_SECRET

Optional plan mapping:
    STRIPE_PRICE_STARTER
    STRIPE_PRICE_PROFESSIONAL
    STRIPE_PRICE_PORTFOLIO

IMPORTANT FOR TENANT RENT:
    Configure the Stripe webhook destination to receive events from connected
    accounts as well as the RentFlow platform account.
"""

from datetime import datetime, timezone
from pathlib import Path
import os
import tomllib

import stripe
from fastapi import FastAPI, Request, HTTPException
from supabase import create_client


app = FastAPI(title="RentFlow Stripe Webhooks")


def _load_local_secrets():
    """Load Codespaces/local Streamlit secrets when present."""
    secrets_path = Path(".streamlit/secrets.toml")
    if not secrets_path.exists():
        return {}
    try:
        with secrets_path.open("rb") as f:
            return tomllib.load(f) or {}
    except Exception:
        return {}


_LOCAL_SECRETS = _load_local_secrets()


def get_config(key, default=None, required=False):
    """
    Render uses environment variables. Local development can fall back to
    .streamlit/secrets.toml. Empty strings are treated as missing.
    """
    value = os.environ.get(key)
    if value is None or str(value).strip() == "":
        value = _LOCAL_SECRETS.get(key, default)

    if isinstance(value, str):
        value = value.strip()

    if required and (value is None or value == ""):
        raise RuntimeError(
            f"Missing required configuration: {key}. "
            "Set it as a Render environment variable or in "
            ".streamlit/secrets.toml for local development."
        )
    return value


SUPABASE_URL = get_config("SUPABASE_URL", required=True)
SUPABASE_SERVICE_ROLE_KEY = get_config(
    "SUPABASE_SERVICE_ROLE_KEY",
    required=True,
)
STRIPE_SECRET_KEY = get_config("STRIPE_SECRET_KEY", required=True)

# The service is allowed to start without the webhook secret so Render can
# assign a public URL first. /stripe/webhook will return 503 until the real
# Stripe endpoint signing secret is configured.
webhook_secret = get_config("STRIPE_WEBHOOK_SECRET")

stripe.api_key = STRIPE_SECRET_KEY
supabase_admin = create_client(
    SUPABASE_URL,
    SUPABASE_SERVICE_ROLE_KEY,
)

PRICE_TO_PLAN = {
    price_id: plan
    for price_id, plan in (
        (get_config("STRIPE_PRICE_STARTER"), "Starter"),
        (get_config("STRIPE_PRICE_PROFESSIONAL"), "Professional"),
        (get_config("STRIPE_PRICE_PORTFOLIO"), "Portfolio"),
    )
    if price_id
}


def object_to_dict(obj):
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    return dict(obj)


def find_user_id(subscription_dict):
    metadata = subscription_dict.get("metadata") or {}
    user_id = metadata.get("user_id")

    if user_id:
        return user_id

    subscription_id = subscription_dict.get("id")
    if subscription_id:
        result = (
            supabase_admin
            .table("owner_profiles")
            .select("user_id")
            .eq("stripe_subscription_id", subscription_id)
            .limit(1)
            .execute()
        )
        if result.data:
            return result.data[0]["user_id"]

    customer_id = subscription_dict.get("customer")
    if customer_id:
        result = (
            supabase_admin
            .table("owner_profiles")
            .select("user_id")
            .eq("stripe_customer_id", customer_id)
            .limit(1)
            .execute()
        )
        if result.data:
            return result.data[0]["user_id"]

    return None


def sync_subscription(subscription):
    sub = object_to_dict(subscription)
    user_id = find_user_id(sub)

    if not user_id:
        return

    plan = None
    try:
        price_id = sub["items"]["data"][0]["price"]["id"]
        plan = PRICE_TO_PLAN.get(price_id)
    except Exception:
        pass

    payload = {
        "subscription_status": sub.get("status"),
        "stripe_customer_id": sub.get("customer"),
        "stripe_subscription_id": sub.get("id"),
        "cancel_at_period_end": bool(
            sub.get("cancel_at_period_end", False)
        ),
    }

    if plan:
        payload["plan"] = plan

    if sub.get("current_period_end"):
        payload["current_period_end"] = datetime.fromtimestamp(
            sub["current_period_end"],
            tz=timezone.utc,
        ).isoformat()

    if sub.get("trial_end"):
        payload["trial_end"] = datetime.fromtimestamp(
            sub["trial_end"],
            tz=timezone.utc,
        ).isoformat()

    (
        supabase_admin
        .table("owner_profiles")
        .update(payload)
        .eq("user_id", str(user_id))
        .execute()
    )


def get_event_connected_account(event):
    """Return Stripe connected account ID from Event.account when present."""
    try:
        account_id = event.get("account")
    except Exception:
        account_id = None

    return str(account_id).strip() if account_id else None


def is_tenant_rent_checkout(session_dict):
    """Identify tenant-rent Checkout Sessions from metadata."""
    metadata = session_dict.get("metadata") or {}

    if metadata.get("payment_kind") == "tenant_rent":
        return True

    required = ("tenant_id", "unit_id", "owner_id", "rent_month")
    return (
        not session_dict.get("subscription")
        and all(metadata.get(key) for key in required)
    )


def verify_owner_connected_account(owner_id, connected_account_id):
    result = (
        supabase_admin
        .table("owner_payment_settings")
        .select("stripe_connect_account_id")
        .eq("owner_id", str(owner_id))
        .limit(1)
        .execute()
    )

    if not result.data:
        raise RuntimeError(
            f"No RentFlow Stripe payment settings found for owner {owner_id}."
        )

    expected = str(
        result.data[0].get("stripe_connect_account_id") or ""
    ).strip()

    if not expected:
        raise RuntimeError(
            f"Owner {owner_id} does not have a Stripe connected account."
        )

    if expected != str(connected_account_id):
        raise RuntimeError(
            "Stripe connected account does not match the RentFlow owner."
        )


def record_tenant_rent_payment(
    session_obj,
    connected_account_id,
    event_created=None,
):
    """
    Record one successful connected-account Checkout Session into RentFlow.

    Database idempotency:
      - application lookup by stripe_session_id
      - existing unique index payments_stripe_session_uidx
    """
    session = object_to_dict(session_obj)
    session_id = str(session.get("id") or "").strip()

    if not session_id:
        raise RuntimeError("Stripe Checkout Session ID is missing.")

    payment_status = str(session.get("payment_status") or "").lower()
    if payment_status != "paid":
        return {
            "processed": False,
            "reason": f"payment_status={payment_status or 'unknown'}",
        }

    metadata = session.get("metadata") or {}
    tenant_id = metadata.get("tenant_id")
    unit_id = metadata.get("unit_id")
    owner_id = metadata.get("owner_id")
    rent_month = metadata.get("rent_month")

    if not tenant_id or not unit_id or not owner_id or not rent_month:
        raise RuntimeError(
            "Tenant-rent Stripe metadata is incomplete. "
            "Expected tenant_id, unit_id, owner_id, and rent_month."
        )

    if not connected_account_id:
        raise RuntimeError(
            "Connected-account Stripe event is missing event.account. "
            "Verify that the Stripe webhook destination is configured to "
            "receive events from connected accounts."
        )

    verify_owner_connected_account(owner_id, connected_account_id)

    existing = (
        supabase_admin
        .table("payments")
        .select("payment_id")
        .eq("stripe_session_id", session_id)
        .limit(1)
        .execute()
    )

    if existing.data:
        return {
            "processed": True,
            "duplicate_payment": True,
            "payment_id": existing.data[0].get("payment_id"),
        }

    amount_paid = float(session.get("amount_total") or 0) / 100.0
    if amount_paid <= 0:
        raise RuntimeError(
            f"Stripe Checkout Session {session_id} has no positive amount_total."
        )

    payment_intent = session.get("payment_intent")
    if isinstance(payment_intent, dict):
        payment_intent = payment_intent.get("id")

    if event_created:
        payment_date = datetime.fromtimestamp(
            int(event_created),
            tz=timezone.utc,
        ).date().isoformat()
    else:
        payment_date = datetime.now(timezone.utc).date().isoformat()

    payload = {
        "tenant_id": str(tenant_id),
        "unit_id": str(unit_id),
        "rent_month": str(rent_month),
        "payment_date": payment_date,
        "amount_paid": amount_paid,
        "payment_method": "Stripe",
        "notes": "Online Tenant Portal payment (Stripe webhook)",
        "stripe_session_id": session_id,
        "stripe_payment_intent_id": (
            str(payment_intent) if payment_intent else None
        ),
    }

    try:
        inserted = (
            supabase_admin
            .table("payments")
            .insert(payload)
            .execute()
        )
    except Exception:
        # Browser-return fallback and webhook can race. The unique index on
        # stripe_session_id remains the final idempotency guard.
        existing_after_race = (
            supabase_admin
            .table("payments")
            .select("payment_id")
            .eq("stripe_session_id", session_id)
            .limit(1)
            .execute()
        )
        if existing_after_race.data:
            return {
                "processed": True,
                "duplicate_payment": True,
                "payment_id": existing_after_race.data[0].get("payment_id"),
            }
        raise

    payment_id = None
    if getattr(inserted, "data", None):
        payment_id = inserted.data[0].get("payment_id")

    return {
        "processed": True,
        "duplicate_payment": False,
        "payment_id": payment_id,
        "amount_paid": amount_paid,
    }


@app.get("/health")
def health():
    return {
        "status": "ok",
        "webhook_secret_configured": bool(webhook_secret),
        "environment": "render" if os.environ.get("RENDER") else "local",
    }


@app.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    if not webhook_secret:
        raise HTTPException(
            status_code=503,
            detail=(
                "STRIPE_WEBHOOK_SECRET is not configured yet. "
                "Create the Stripe webhook endpoint, then add its signing "
                "secret to the Render service."
            ),
        )

    payload = await request.body()
    signature = request.headers.get("stripe-signature")

    if not signature:
        raise HTTPException(
            status_code=400,
            detail="Missing Stripe-Signature header.",
        )

    try:
        event = stripe.Webhook.construct_event(
            payload,
            signature,
            webhook_secret,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid webhook: {exc}",
        )

    event_id = event["id"]

    # Idempotency
    existing = (
        supabase_admin
        .table("stripe_webhook_events")
        .select("event_id")
        .eq("event_id", event_id)
        .limit(1)
        .execute()
    )

    if existing.data:
        return {"received": True, "duplicate": True}

    event_type = event["type"]
    obj = event["data"]["object"]

    if event_type in (
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.deleted",
    ):
        sync_subscription(obj)

    elif event_type in (
        "checkout.session.completed",
        "checkout.session.async_payment_succeeded",
    ):
        session = object_to_dict(obj)
        if is_tenant_rent_checkout(session):
            connected_account_id = get_event_connected_account(event)
            record_tenant_rent_payment(
                session,
                connected_account_id,
                event_created=event.get("created"),
            )
        else:
            subscription_id = session.get("subscription")
            if subscription_id:
                subscription = stripe.Subscription.retrieve(subscription_id)
                sync_subscription(subscription)

    elif event_type in (
        "invoice.payment_failed",
        "invoice.paid",
    ):
        invoice = object_to_dict(obj)
        subscription_id = invoice.get("subscription")
        if subscription_id:
            subscription = stripe.Subscription.retrieve(subscription_id)
            sync_subscription(subscription)

    (
        supabase_admin
        .table("stripe_webhook_events")
        .insert({
            "event_id": event_id,
            "event_type": event_type,
        })
        .execute()
    )

    return {
        "received": True,
        "event_type": event_type,
    }
