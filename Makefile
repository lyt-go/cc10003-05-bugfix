.PHONY: install install-dev backend frontend test backend-test frontend-typecheck

install:
	cd backend && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
	cd frontend && npm install

install-dev:
	cd backend && .venv/bin/pip install -r requirements-dev.txt

backend:
	cd backend && ./run.sh

frontend:
	cd frontend && npm run dev

test: backend-test frontend-typecheck

backend-test:
	cd backend && .venv/bin/python -m unittest discover -s tests -v

frontend-typecheck:
	cd frontend && npm run typecheck
