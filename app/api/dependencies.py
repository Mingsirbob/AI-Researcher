from __future__ import annotations

from fastapi import Request

from ..container import AppContainer


def get_container(request: Request) -> AppContainer:
    return request.app.state.container


def get_research_store(request: Request):
    return get_container(request).research_store


def get_quant_store(request: Request):
    return get_container(request).quant_store


def get_paper_trading_service(request: Request):
    return get_container(request).paper_trading_service


def get_ifind_service(request: Request):
    return get_container(request).ifind_service
