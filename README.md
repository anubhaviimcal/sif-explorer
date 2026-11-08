# SIF Explorer: home screen web app + data pipeline

```
docs/                the web app (host this folder)
  index.html, sw.js, manifest.webmanifest, icons/
  data/sif.json      the data the app loads on launch (sample data until the pipeline runs)
sif_pipeline.py      builds fresh data from AMFI
config.json, manual_fields.csv
.github/workflows/   nightly automation (GitHub)
```

## 1. Put the app online (needs HTTPS, which these hosts provide)
**Automatic daily updates (recommended): GitHub Pages**
1. Create a GitHub repository and upload everything in this folder. A free account needs a public repo for Pages.
2. Settings > Pages > deploy from branch `main`, folder `/docs`. Your app will be at `https://<user>.github.io/<repo>/`.
3. Do the pipeline setup below. The included workflow then refreshes `docs/data/sif.json` every night.

**Quick test without GitHub:** drag the `docs` folder onto a static host such as Netlify Drop or Cloudflare Pages. Data then updates only when you replace `data/sif.json` yourself.

## 2. Install on Android
Open the web address in Chrome, then use the in-app "Install app" button or Chrome's menu > Install app / Add to Home screen. It opens full screen like an app and keeps working offline with the last data it loaded.

## 3. Pipeline setup (one time)
1. In `config.json`, paste AMFI's latest SIF NAV text file link into `latest_nav_url`. For `history_url_template`, copy the NAV history request URL from your browser's Network tab and replace the two dates with `{from}` and `{to}` (format 31-Oct-2026). History is optional: the cache also builds up with each daily run.
2. `python sif_pipeline.py inspect <url or file>` should show AMCs, categories and Regular-plan Growth rows. If it looks wrong, share the output.
3. `python sif_pipeline.py selftest` should print "selftest passed".
4. `python sif_pipeline.py run` writes `output/sif.json` and `output/report.txt`. Copy `output/sif.json` to `docs/data/sif.json` (the workflow does this for you).

Each month, fill the blank rows the pipeline adds to `manual_fields.csv` (expense ratio, exit load, fund manager, source link; launch date, AMC and category if AMFI's headings are wrong). Read `report.txt` for warnings and spot-check a few CAGRs against AMC factsheets.

## 4. Changing the app later
After you edit `docs/index.html`, change `V = "sif-v1"` in `docs/sw.js` to `sif-v2` so installed copies pick up the update.

## Notes
- The app does not rank funds. Sorting is user-chosen and disclaimers are shown next to the list. Have your compliance advisor review it before launch.
- AMFI says its information comes from members and it takes no responsibility for its accuracy. Check AMFI's terms of use before automating downloads or reusing the data commercially.
- Expense ratio is manual for now.
