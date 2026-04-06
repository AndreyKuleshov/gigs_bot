# RFC: Telegram Bot + Google Calendar + Gemini LLM

Status: Draft  
Date: 2026-04-05  
Version: 1.0  

## Overview
Telegram-бот для управления Google Calendar с двумя режимами:
- Button mode (без LLM)
- Free-text mode (Gemini LLM)

CRUD: create, read, update, delete

## Stack
- Python 3.11+
- FastAPI
- aiogram
- PostgreSQL
- Redis
- Gemini (LLM)
- Google Calendar API

## Architecture
Telegram → aiogram → FastAPI → (LLM Agent / Calendar Service / Auth) → Google API

## Modes
### Button Mode
- Детерминированная логика
- FSM (aiogram)
- CRUD через кнопки

### Free-text Mode
User → Gemini → intent + entities → tool → result

## Intents
- create
- read
- update
- delete

## Tools
create_event, read_events, update_event, delete_event

## Rule
LLM не знает event_id → всегда сначала read_events

## Components
- Bot (aiogram)
- Backend (FastAPI)
- Gemini Agent
- Calendar Service
- Auth (OAuth2)

## Security
- OAuth2
- encrypted tokens
- tool validation

## Implementation Plan
1. CRUD через кнопки
2. Gemini agent
3. Улучшения

## Constraints
- LLM без прямого API доступа
- строгая валидация
- async код
- модульная структура

## Done
- CRUD работает
- LLM работает
- OAuth работает
