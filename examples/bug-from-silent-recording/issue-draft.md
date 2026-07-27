## Checkpoint

- **Heard:** silent recording — no audio track (`transcript.available: false`, reason: "no audio stream in recording").
- **Saw:** Checkout page for "Nimbus ANC Headphones" (€100.00). Coupon `SAVE20` applied → −€20.00 → Total €80.00. Clicking "Pay €80.00" triggers `POST /api/pay {"amount": 80, "currency": "eur"}`, followed by dev-log line `gateway 402 amount_too_small - minimum charge is 100 (cents); got 80`, and the UI shows "Payment failed · Reference: req_7F3A / Your card was not charged."
- **When:** t_wall `2026-07-27T16:29:07+02:00` (POST) / `2026-07-27T16:29:08+02:00` (failure) — confidence **low** (wall clock resolved from file mtime, not an embedded recording timestamp).
- **Expected:** Payment should succeed for a legitimate €80.00 order. The gateway rejects it as "too small" because the request sends `"amount": 80` — read as 80 **cents** (€0.80) by a minor-unit payment gateway — instead of `8000` cents for €80.00.

---

## Issue Draft

**Title:** Checkout payment fails after applying a coupon — `/api/pay` sends amount in euros instead of cents, triggering gateway `amount_too_small` error

**Observed**
On the checkout page for "Nimbus ANC Headphones" (€100.00 subtotal), applying coupon `SAVE20` correctly discounts the total to €80.00 and shows "✓ SAVE20 applied". Clicking "Pay €80.00" shows "Processing…" then fails with "Payment failed · Reference: req_7F3A — Your card was not charged." The on-screen dev log shows the actual request/response:
```
16:29:07 POST /api/pay {"amount": 80, "currency": "eur"}
16:29:08 gateway 402 amount_too_small - minimum charge is 100 (cents); got 80
16:29:08 payment failed · ref=req_7F3A
```

**Expected**
The €80.00 total should be charged successfully. The request sends `amount: 80`, which the gateway interprets as 80 **cents** (€0.80) — below its 100-cent minimum. For a €80.00 charge the request should send `amount: 8000` (cents). This points to a euros-vs-cents unit mismatch in how `/api/pay` builds the charge amount.

**Reproduction steps**
1. Open checkout for "Nimbus ANC Headphones" (€100.00).
2. Enter coupon code `SAVE20` and click Apply — total becomes €80.00, "✓ SAVE20 applied" shown.
3. Click "Pay €80.00".
4. Button shows "Processing…", then the page displays "Payment failed · Reference: req_7F3A — Your card was not charged."

**Severity:** P1 — checkout/payment flow is broken end-to-end for this order; the customer cannot complete a valid purchase and gets a hard payment failure.

**Evidence**
- Frame `t00007040.jpg` (t_ms=7040, t_wall=2026-07-27T16:29:08+02:00): OCR `"16:29:07 POST /api/pay {\"amount\": 80, \"currency\": \"eur\"}"`
- Frame `t00007960.jpg` (t_ms=7960, t_wall=2026-07-27T16:29:08+02:00): OCR `"16:29:08 gateway 402 amount_too_small - minimum charge is 100 (cents); got 80"` and `"16:29:08 payment failed· ref=req_7F3A"`; UI text "Payment failed · Reference: req_7F3A / Your card was not charged."
- job_id: `8703a66bbe77a7d0`, source: `checkout-coupon-bug.mp4` (silent, 15.12s)

*Note: wall-clock times above have low confidence (derived from file mtime, not an embedded recording timestamp) — treat as approximate if correlating with server logs.*
