STRATEGY = {
    "name": "Daily volume breakout",
    "description": "Bullish close above the prior range with elevated volume.",
    "parameters": [
        {"key": "volume_period", "label": "Volume lookback", "type": "integer", "default": 20, "min": 5, "max": 100},
        {"key": "volume_multiple", "label": "Volume multiple", "type": "number", "default": 3.0, "min": 1.0, "max": 10.0},
        {"key": "price_period", "label": "Price lookback", "type": "integer", "default": 20, "min": 5, "max": 100, "advanced": True},
        {"key": "stop_percent", "label": "Stop loss %", "type": "number", "default": 2.0, "min": 0.25, "max": 10.0, "advanced": True},
        {"key": "target_percent", "label": "Target %", "type": "number", "default": 4.0, "min": 0.5, "max": 30.0, "advanced": True},
    ],
}


def evaluate(candles, params):
    if len(candles) <= params["price_period"]:
        return {"signal": False}

    latest = candles[-1]
    prior_high = highest(candles[:-1], "high", params["price_period"])
    volume_strength = volume_multiple(candles, params["volume_period"])
    if latest["close"] > latest["open"] and latest["close"] > prior_high and volume_strength >= params["volume_multiple"]:
        entry = latest["close"] * 1.005
        return {
            "signal": True,
            "entry_price": entry,
            "stop_loss": entry * (1 - params["stop_percent"] / 100),
            "target_price": entry * (1 + params["target_percent"] / 100),
            "reason": "Bullish close broke the prior high with elevated volume.",
        }
    return {"signal": False}
