STRATEGY = {
    "name": "Kite connection check",
    "description": "Checks strategy upload and evaluation using Kite daily candles.",
    "parameters": [
        {"key": "min_price", "label": "Minimum price", "type": "number", "default": 1.0, "min": 1.0, "max": 100000.0},
        {"key": "entry_buffer", "label": "Entry buffer %", "type": "number", "default": 1.0, "min": 0.6, "max": 5.0},
        {"key": "stop_percent", "label": "Stop loss %", "type": "number", "default": 2.0, "min": 1.0, "max": 10.0},
        {"key": "target_percent", "label": "Target %", "type": "number", "default": 4.0, "min": 1.0, "max": 20.0},
        {"key": "min_volume", "label": "Minimum daily volume", "type": "integer", "default": 0, "min": 0, "max": 1000000000, "advanced": True},
    ],
}


def evaluate(candles, params):
    if len(candles) < 25:
        return {"signal": False}

    latest = candles[-1]
    if latest["close"] < params["min_price"] or latest["volume"] < params["min_volume"]:
        return {"signal": False}

    entry = latest["close"] * (1 + params["entry_buffer"] / 100)
    return {
        "signal": True,
        "entry_price": entry,
        "stop_loss": entry * (1 - params["stop_percent"] / 100),
        "target_price": entry * (1 + params["target_percent"] / 100),
        "reason": "Connection check: Kite daily candles passed the configured price and volume filters.",
    }
