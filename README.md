# Power BI Model Analyzer (Phase 1)

Upload a `.pbix` / `.pbit` → plain-English explanation of every Power Query step, relationships, DAX → Excel export.

## Run locally
    pip install -r requirements.txt
    streamlit run app.py

## Deploy free
1. Push this folder to a GitHub repo.
2. Go to share.streamlit.io, connect the repo, set main file to `app.py`.

## Outputs
- On screen: step-by-step explanation with Before / After for every Power Query step.
- Word (.docx): formatted documentation (opens in Word and Google Docs).
- Excel: all tables + explained steps + Gold layer to-do list.

## What's inside
- `m_explainer.py`: Power Query (M) → plain English, before/after tracing, Gold-layer advice
- `dax_explainer.py`: DAX → plain English, dependencies, performance findings, Gold-layer advice
- `doc_export.py`: formatted Word document (python-docx); `doc_preview.py`: same content shown in the app
