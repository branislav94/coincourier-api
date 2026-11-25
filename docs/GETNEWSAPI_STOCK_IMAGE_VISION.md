# DEV stock-image Vision pilot

Cloud Vision checks the prepared crop of an eligible V2 emergency stock candidate after strict selection has finished. Stock eligibility and credits still come from the existing provider and WordPress checks. The request contains one metadata-free JPEG preview with a long edge of at most 768 px and one Label Detection feature. CoinCourier evaluates the headline and lead locally. [Featured image rescue](GETNEWSAPI_FEATURED_IMAGE_RESCUE.md) · [Google Label Detection](https://docs.cloud.google.com/vision/docs/labels)

The pilot looks for two conflicts. A Coldcard, hardware-wallet or recovery-seed article can conflict with `dandelion` or a similarly specific botanical label. A financial-record or record ETF-flow article can conflict with `vinyl record`, `record player` or `turntable`. The conflict threshold is `0.90`. Plausible on-topic labels such as `usb flash drive`, `electronic device`, `market chart` or `financial document` veto a conflict at `0.70` or higher. Mixed topics and generic labels alone yield no rejection. A score expresses Google's confidence in its label. For example, a USB label leaves the exact hardware model unverified.

## Deploy in shadow mode

1. Choose a billing-enabled Google Cloud project and enable the Cloud Vision API. Create a server-side API key with an API restriction for Cloud Vision (`vision.googleapis.com`). Add the server's outbound IP to the key's application restrictions when its IP is stable. Store the key as the Dokploy secret `GOOGLE_CLOUD_VISION_API_KEY`. [Cloud Vision setup](https://docs.cloud.google.com/vision/docs/setup) · [API key restrictions](https://docs.cloud.google.com/docs/authentication/api-keys-best-practices)
2. Set the following in the existing DEV resource. Retain its V2 stock configuration and existing `/data` mount. [Dokploy DEV handoff](GETNEWSAPI_DOKPLOY_DEV_HANDOFF.md)

   ```dotenv
   STOCK_IMAGE_VISION_MODE=shadow
   STOCK_IMAGE_VISION_MONTHLY_UNIT_LIMIT=100
   STOCK_IMAGE_SUBJECT_MODE=shadow
   ```

3. Deploy the reviewed `dev` revision and restart DEV through the normal deployment. Record the deployed SHA, image digest and UTC start time. Run the probe in `getnewsapi-dev` with the same `WRITABLE_STATE_DIR=/data` as the publisher and its scheduled jobs. Keep `/data/stock_image_vision.sqlite3` on the persistent volume across restarts and stock-cache cleanups. Publisher and probe share this accounting store.

Shadow preserves the baseline image, credit and fallback. `STOCK_IMAGE_VISION_MODE=enforce` skips candidates on clear conflicts, then continues the existing rescue and fallback chain. Roll back with `STOCK_IMAGE_VISION_MODE=off` and a restart. Keep the independent subject pilot in `shadow`.

## Probe the three published JPEGs

In the DEV service's terminal, download the JPEGs to `/tmp` and run the probes from `/app`.

```bash
cd /app
python -B - <<'PY'
from pathlib import Path
from urllib.request import urlopen

base = 'https://dev.coincourier.io/wp-content/uploads/2026/11/'
for post_id, filename in (
    (6423, 'pixabay-search-image-80.jpg'),
    (6435, 'pixabay-search-image-81.jpg'),
    (6420, 'pexels-search-image-120.jpg'),
):
    with urlopen(base + filename, timeout=20) as image:
        Path(f'/tmp/cc-{post_id}.jpg').write_bytes(image.read())
PY

python -B vision_label_probe.py --image /tmp/cc-6423.jpg --title 'Coinkite Details Coldcard Seed Flaw Missed in Reviews' --live
python -B vision_label_probe.py --image /tmp/cc-6435.jpg --title 'Coinkite Details Coldcard Seed Generation Flaw in November 2026 Postmortem' --live
python -B vision_label_probe.py --image /tmp/cc-6420.jpg --title 'Coinkite Details Coldcard Firmware RNG Configuration Error' --live
```

Omit `--live` to verify local image preparation without a Google request or accounting-store access. The probe runs outside the publishing pipeline and writes one JSON record with the verdict, reason and up to ten normalized labels and scores. `status=submitted` uses one reserved unit, including on a timeout. `status=cache_hit` reuses evidence and reserves zero units. Repeat a successful probe with the same JPEG to verify a cache hit. The cache key uses the exact preview, so the published JPEG and the original stock download can produce different keys. An `in_flight` result means another worker already reserved the same preview. Use the `reason` field when an enabled probe returns without usable labels.

| DEV post | Stock asset | Human judgment |
| --- | --- | --- |
| 6423 | Pixabay 1346727 | Dandelion seed image mismatches the Coldcard seed-flaw story. |
| 6435 | Pixabay 1514032 | Dandelion image mismatches the Coldcard postmortem. |
| 6420 | Pexels 3631991 | Bitcoin-themed USB device is an acceptable illustration. The exact hardware model is unverified. |

Record the actual Google labels and verdict beside each human judgment. If Vision misses either dandelion or flags the USB control, report the result before changing the matching rule. Automated tests use generated images and mocked labels.

## Review 50 consecutive DEV articles

After the three probes, collect 50 consecutive distinct articles reaching stock selection. Use `article` in `[IMG-V2] image` to define the cohort, including searches with zero results. Join `[IMG-VISION]` decisions, `[IMG-RESCUE]` outcomes, `[IMG-V2] image_terminal` and the final WordPress `featured_media`.

Report each of the following separately.

- Articles reaching a callback with a supported topic and an otherwise uploadable rescue candidate. Count distinct `article` values in the Vision decisions.
- Actual Google submissions and cache hits. Count candidate decisions by `status`, and record reserved units and unavailable checks separately from label evidence.
- Would-rejects, confirmed misses and false rejections. Review the actual candidate image with its headline and lead. Count clear mismatches the labels missed and acceptable photos flagged in shadow. Record checks with unavailable evidence separately, including any clear mismatches among them.
- Vision latency, recording `latency_ms` separately for submissions and cache hits. Include median and 95th percentile with their sample sizes.
- Missing featured images and generation use. Record Grok and OpenAI generation separately, including attempts when available and the final image source. If enforcement is later enabled, track additional generation and its cost separately from Vision.

Review the photos and counts before any manual mode change. Keep DEV in shadow until this review supports enforcement.

## Usage and cost

Each outbound submission reserves one CoinCourier unit atomically before its request. Publisher attempts share a two-submission limit per article, including retries. The probe shares the monthly ceiling and evidence cache. The monthly ceiling accepts `0` to `2000` and uses midnight Pacific time on the first of the month. The ceiling applies to CoinCourier submissions, while Google prices usage across the billing account by SKU. At standard USD rates, 2,000 Label Detection units cost about $1.50 with the full 1,000 free units available, or about $3.00 if the billing account has already consumed that free tier. A Cloud Billing alert monitors spending and the CoinCourier ceiling limits submissions. [Cloud Vision pricing](https://cloud.google.com/vision/pricing) · [Cloud Billing pricing tiers](https://docs.cloud.google.com/billing/docs/how-to/pricing-table) · [Pacific time reset](https://docs.cloud.google.com/billing/docs/how-to/export-data-bigquery-tables/pricing-data)
