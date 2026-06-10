.PHONY: build up start down clean logs ps help

## Build all images (must run before `make up`)
build:
	@echo "Building computer-use base image..."
	docker build -t computeagent/computer-use-base:latest ./computer-use-demo
	@echo "Building session-agent image..."
	docker build -t computeagent/session-agent:latest ./session_agent
	@echo "Building backend image..."
	docker build -t computeagent/backend:latest ./backend
	@echo "All images built."

## Start the full stack (builds first)
up: build
	docker compose up -d
	@echo "Stack is up."
	@echo "   Frontend  -> http://localhost"
	@echo "   API docs  -> http://localhost:8000/docs"

## Start without rebuilding
start:
	docker compose up -d

## Stop and remove containers
down:
	docker compose down

## Stop, remove containers AND volumes (full reset)
clean:
	docker compose down -v
	docker rmi -f computeagent/backend:latest computeagent/session-agent:latest computeagent/computer-use-base:latest 2>/dev/null || true
	@echo "Clean complete."

## Follow logs
logs:
	docker compose logs -f backend

## Show running containers
ps:
	docker compose ps

## Show help
help:
	@grep -E '^##' Makefile | sed 's/## //'
