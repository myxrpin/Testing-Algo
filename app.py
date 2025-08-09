# app.py
import os
import time
import json
import logging
from flask import Flask, request, jsonify
from binance.client import Client
from binance.enums import TIME_IN_FORCE_GTC, ORDER_TYPE_LIMIT, ORDER_TYPE_MARKET

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("tv-binance-bot")

# ========== CONFIG from environment ==========
API_KEY = os.getenv("WMi5r5amHglmbWeWOzcdmIMKoOCtpfr8stZA9MW2NZcTQFfXjTP2ZOsLurnniHHo", "")
API_SECRET = os.getenv("Rpd0ibB2vLPWYnvEuYiZq47uAriOt0M7OMJkEpIdNsCQt47QKk1R7RbxVsMG1QJ9", "")
# Optional secret token to validate TradingView -> set same token in Pine alert JSON or header
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", None)
# Set "true" to use Binance Futures TESTNET (test keys must be from futures testnet)
USE_TESTNET = os.getenv("USE_TESTNET", "true").lower() in ("1", "true", "yes")

# Defaults / timeouts
FILL_TIMEOUT = int(os.getenv("FILL_TIMEOUT_SECONDS", "30"))  # how long to wait for a LIMIT to fill (seconds)
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "1.0"))

# ========== Flask app ==========
app = Flask(__name__)

# ========== Binance client init ==========
if not API_KEY or not API_SECRET:
    log.warning("BINANCE_API_KEY or BINANCE_API_SECRET not set in environment - script will fail on orders.")

client = Client(API_KEY, API_SECRET)

# Configure client base URL for futures testnet if requested
if USE_TESTNET:
    # NOTE: python-binance doesn't directly expose a boolean for futures testnet endpoints,
    # so we override the API URL for futures calls.
    client.API_URL = "https://testnet.binancefuture.com/fapi/v1"
    log.info("Using Binance FUTURES TESTNET endpoint")
else:
    # live futures endpoint
    client.API_URL = "https://fapi.binance.com/fapi/v1"
    log.info("Using Binance FUTURES LIVE endpoint")

# ========== Helpers ==========
def validate_webhook(req):
    """Optional secret validation. TradingView can send a 'secret' field in JSON or header 'X-SIGNAL-TOKEN'."""
    if not WEBHOOK_SECRET:
        return True  # no secret configured -> accept
    token = req.headers.get("X-SIGNAL-TOKEN") or (req.json.get("secret") if req.is_json else None)
    if token and token == WEBHOOK_SECRET:
        return True
    return False

def wait_for_order_fill(symbol, order_id, timeout=FILL_TIMEOUT, poll=POLL_INTERVAL):
    """Poll order status until FILLED or timeout. Returns True if FILLED."""
    end = time.time() + timeout
    while time.time() < end:
        try:
            status_resp = client.futures_get_order(symbol=symbol, orderId=order_id)
            status = status_resp.get("status", "")
            log.info(f"Order {order_id} status: {status}")
            if status == "FILLED":
                return True, status_resp
            if status in ("CANCELED", "REJECTED", "EXPIRED"):
                return False, status_resp
        except Exception as e:
            log.warning("Error fetching order status: %s", e)
        time.sleep(poll)
    return False, {"status": "TIMEOUT"}

def place_tp_sl_after_fill(symbol, side, tp_price, sl_price, qty):
    """Place TAKE_PROFIT_MARKET and STOP_MARKET with closePosition=True to only close"""
    try:
        # TP: TAKE_PROFIT_MARKET (market close when TP reached)
        tp_side = "SELL" if side.upper() == "BUY" else "BUY"
        tp_order = client.futures_create_order(
            symbol=symbol,
            side=tp_side,
            type="TAKE_PROFIT_MARKET",
            stopPrice=str(tp_price),
            closePosition=True
        )
        log.info("Placed TP order: %s", tp_order)

        # SL: STOP_MARKET (market close when SL hit)
        sl_order = client.futures_create_order(
            symbol=symbol,
            side=tp_side,
            type="STOP_MARKET",
            stopPrice=str(sl_price),
            closePosition=True
        )
        log.info("Placed SL order: %s", sl_order)

        return {"tp": tp_order, "sl": sl_order}
    except Exception as e:
        log.exception("Failed to place TP/SL: %s", e)
        raise

# ========== Webhook endpoint ==========
@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        if not request.is_json:
            return jsonify({"status": "error", "message": "Expected JSON"}), 400

        payload = request.get_json()
        log.info("Received payload: %s", payload)

        if not validate_webhook(request):
            log.warning("Webhook secret mismatch")
            return jsonify({"status": "error", "message": "Invalid webhook secret"}), 403

        # required fields from your Pine JSON
        try:
            symbol = payload["symbol"]               # e.g., "ETHUSDT"
            side = payload["side"].upper()           # "BUY" or "SELL"
            entry_price = float(payload["entry"])    # price for limit entry OR reference
            sl = float(payload["sl"])
            tp = float(payload["tp"])
            qty = float(payload["qty"])
        except KeyError as ke:
            return jsonify({"status": "error", "message": f"Missing field {ke}"}), 400
        except Exception as e:
            return jsonify({"status": "error", "message": f"Invalid field types: {e}"}), 400

        # optional: allow TradingView to request MARKET vs LIMIT by sending orderType
        order_type = payload.get("orderType", "LIMIT").upper()  # "LIMIT" or "MARKET"
        clientOrderId = payload.get("clientOrderId")  # optional idempotency token

        # Place Entry
        log.info("Placing entry: %s %s qty=%s at %s (%s)", side, symbol, qty, entry_price, order_type)
        if order_type == "MARKET":
            entry_order = client.futures_create_order(
                symbol=symbol,
                side=side,
                type="MARKET",
                quantity=qty,
                newClientOrderId=clientOrderId
            )
            log.info("Entry order response: %s", entry_order)
            filled = True
            entry_resp = entry_order
        else:
            # LIMIT entry
            entry_order = client.futures_create_order(
                symbol=symbol,
                side=side,
                type="LIMIT",
                timeInForce=TIME_IN_FORCE_GTC,
                price=str(entry_price),
                quantity=qty,
                newClientOrderId=clientOrderId
            )
            log.info("Limit entry placed: %s", entry_order)
            order_id = entry_order.get("orderId") or entry_order.get("clientOrderId")
            filled, entry_resp = wait_for_order_fill(symbol, order_id)

        if not filled:
            log.warning("Entry not filled within timeout. Entry resp: %s", entry_resp)
            return jsonify({"status": "error", "message": "Entry not filled", "entry_response": entry_resp}), 409

        # Place TP/SL that CLOSE position only
        results = place_tp_sl_after_fill(symbol, side, tp, sl, qty)
        return jsonify({"status": "ok", "entry": entry_resp, "tp_sl": results})

    except Exception as e:
        log.exception("Unhandled error in webhook")
        return jsonify({"status": "error", "message": str(e)}), 500

# optional healthcheck
@app.route("/", methods=["GET"])
def index():
    return "OK - TradingView -> Binance webhook"

if __name__ == "__main__":
    # local debug only; in Render use gunicorn main:app --bind 0.0.0.0:$PORT
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=False)
