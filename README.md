# Liquidity-Aware Procurement Agent

The application combines the existing DuckDB demo data and CP-SAT optimizer with a resumable LangGraph workflow, a FastAPI backend, and a Streamlit/AG Grid dashboard. PayPal credentials and API calls stay on the server.

## Run locally

1. Create and activate a Python 3.12 virtual environment.
2. Install dependencies with `pip install -r requirements.txt`.
3. Copy `.env.example` to `.env`, set a random `APPROVAL_API_TOKEN` of at least 32 characters, and set PayPal Sandbox credentials. Use the same owner token for the API and dashboard. Gemini is optional; the app uses a deterministic explanation when no Gemini key is configured or the Gemini call fails.
4. Start the backend: `uvicorn main:app --reload --port 8000`.
5. Start the dashboard in another terminal: `streamlit run dashboard.py`.

If `DATABASE_PATH` does not exist, the backend initializes the checked-in schema and seeds the existing demo dataset. Existing databases are left intact. LangGraph checkpoints are written to `LANGGRAPH_CHECKPOINT_PATH`, which should be on persistent storage outside local demos.

## Workflow

`POST /api/agent/runs` detects delayed or overdue receivables, analyzes liquidity, runs the existing procurement optimizer, and creates a plain-language explanation. A valid purchase recommendation pauses at a LangGraph human approval interrupt. Approval creates one PayPal Sandbox order per selected supplier; rejection records the rejection without contacting PayPal. The owner follows each PayPal approval link and then explicitly captures the authorized payment in the dashboard. `GET /api/agent/runs/{run_id}` retrieves the saved workflow state.

The REST API is documented at `/docs` when the backend is running. Business and transaction endpoints under `/api` require the `X-Approval-Token` header. `/health` is the minimal public health check.

## Render

`render.yaml` provisions an API service and a dashboard service. The API service stores the DuckDB database and SQLite LangGraph checkpoints on its persistent disk. Set the PayPal credentials, optional Gemini API key, and the same strong `APPROVAL_API_TOKEN` on both services in Render. The dashboard forwards this token only from its server process; it is not included in browser JavaScript. Update the PayPal return and cancel URLs if the dashboard service receives a different public hostname.

Render deployment requires a paid service plan for persistent disks. The first deployment initializes the demo schema and seed data if the configured database file is absent. Sandbox credentials and live PayPal checkout have not been validated by the automated tests.

## Tests

Run the full offline suite with `python -m unittest discover -s tests -v`. PayPal HTTP calls in integration tests are mocked; no sandbox charge is made by the test suite.
