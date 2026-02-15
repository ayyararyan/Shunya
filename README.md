# SHUNYA Website

A Zen-inspired landing website for the SHUNYA framework.

## Run locally

Open `index.html` directly in your browser, or serve this folder:

```bash
python3 -m http.server 4173
```

Then visit `http://localhost:4173`.

## Deploy on GitHub Pages

This repo includes an automatic Pages workflow at:

- `.github/workflows/deploy-pages.yml`

### One-time setup (GitHub UI)

1. Go to **Settings → Pages** in your GitHub repository.
2. Under **Build and deployment**, choose **Source: GitHub Actions**.
3. Make sure your default branch is `main` (or adjust the workflow trigger branch if needed).

### Deploy

- Push to `main` and GitHub Actions will publish the site.
- Or manually run the workflow from the **Actions** tab.

After deployment, your site URL will be:

`https://<your-github-username>.github.io/<your-repo-name>/`

## Logo asset

The hero uses this logo file path:

`assets/file_0000000098a072029bb52552b3e1abf7.png`

If that file is missing, the site falls back to `assets/shunya-logo.svg`.
