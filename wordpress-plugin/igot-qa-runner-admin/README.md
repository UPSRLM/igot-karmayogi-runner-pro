# iGot QA Runner Admin Plugin

This plugin adds a WordPress admin page for submitting and monitoring jobs against the hosted iGot QA runner API.

## Features

- Stores hosted API base URL and bearer token in WordPress settings
- Submits new runs from wp-admin
- Allows optional per-run Groq and Gemini keys without storing them in WordPress
- Shows recent run statuses by polling the hosted API server-side
- Downloads artifacts through a WordPress admin proxy so the API token is not exposed in the browser

## Install

1. Copy the `igot-qa-runner-admin` folder into `wp-content/plugins/`
2. Activate `iGot QA Runner Admin` from the WordPress plugins screen
3. Open `wp-admin -> iGot QA Runner`
4. Enter the hosted API base URL and bearer token
5. Submit runs from the admin page

## Expected Hosted API

The plugin expects the FastAPI service in this repo with these endpoints:

- `POST /api/runs`
- `GET /api/runs/{run_id}`
- `GET /api/runs/{run_id}/artifacts/{artifact_path}`

## Security Notes

- The API token is stored in WordPress options and used only for server-side requests
- The saved API token is not rendered back into the admin form; leaving the token field blank keeps the existing saved token
- Per-run Groq and Gemini keys are sent to the hosted API for that run only and are not stored by this plugin
- The plugin requires `manage_options` capability and uses WordPress nonces for all actions
