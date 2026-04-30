.PHONY: run validate clean

run:
	uv run main.py

validate:
	uv run validate.py

clean:
	rm -rf specs ledgers metrics.json critiques.json report.md data_manifest.json llm_calls.jsonl
