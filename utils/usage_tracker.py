"""
Usage Tracking for Voice Agent Calls

This module provides a lightweight, non-blocking usage collector that tracks
resource consumption (tokens, characters, seconds) during voice calls.

Usage is tracked per-component with the following units:
- LLM: tokens (prompt_tokens, completion_tokens)  
- TTS: characters (text length sent to TTS engine)
- STT: seconds (audio duration processed)
- Telephony: seconds (call duration)

Copied from: utils/usage_tracker.py
"""

from __future__ import annotations

import logging
import os
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from db.connection_pool import get_db_connection, return_connection
from db.schema_constants import BILLING_PRICING_CATALOG_FULL

if TYPE_CHECKING:
    from livekit.agents import AgentSession

logger = logging.getLogger(__name__)


def is_component_tracking_enabled() -> bool:
    """Check if detailed component cost tracking is enabled"""
    return os.getenv("ENABLE_COMPONENT_COST_TRACKING", "false").lower() in ("true", "1", "yes")


@dataclass
class UsageRecord:
    """Single usage record for a component"""
    provider: str
    model: str
    component: str  # 'stt', 'llm_prompt', 'llm_completion', 'tts', 'telephony'
    unit: str       # 'seconds', 'tokens', 'characters'
    amount: float
    
    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "component": self.component,
            "unit": self.unit,
            "amount": round(self.amount, 6),
        }


@dataclass
class ComponentConfig:
    """Configuration for a specific component (provider + model)"""
    provider: str
    model: str


@dataclass 
class UsageCollector:
    """
    Lightweight, non-blocking usage accumulator for voice calls.
    
    Collects usage metrics during a call and provides a summary for
    database storage at call end.
    """
    
    llm_config: ComponentConfig = field(default_factory=lambda: ComponentConfig("unknown", "unknown"))
    tts_config: ComponentConfig = field(default_factory=lambda: ComponentConfig("unknown", "unknown"))
    stt_config: ComponentConfig = field(default_factory=lambda: ComponentConfig("deepgram", "nova-3"))
    _usage_stats: defaultdict = field(default_factory=lambda: defaultdict(float))
    call_log_id: str | None = None
    
    def set_llm_config(self, provider: str, model: str) -> None:
        """Set LLM provider and model for this call"""
        self.llm_config = ComponentConfig(provider, model)
        logger.debug(f"UsageCollector: LLM config set to {provider}/{model}")
    
    def set_tts_config(self, provider: str, model: str) -> None:
        """Set TTS provider and model for this call"""
        self.tts_config = ComponentConfig(provider, model)
        logger.debug(f"UsageCollector: TTS config set to {provider}/{model}")
    
    def set_stt_config(self, provider: str, model: str) -> None:
        """Set STT provider and model for this call"""
        self.stt_config = ComponentConfig(provider, model)
        logger.debug(f"UsageCollector: STT config set to {provider}/{model}")
    
    # =========================================================================
    # ACCUMULATOR METHODS
    # =========================================================================
    
    def add_llm_tokens(self, prompt_tokens: int, completion_tokens: int) -> None:
        """Add LLM token usage"""
        if prompt_tokens > 0:
            key = (self.llm_config.provider, self.llm_config.model, "llm_prompt", "tokens")
            self._usage_stats[key] += prompt_tokens
        if completion_tokens > 0:
            key = (self.llm_config.provider, self.llm_config.model, "llm_completion", "tokens")
            self._usage_stats[key] += completion_tokens
    
    def add_tts_characters(self, character_count: int) -> None:
        """Add TTS character usage"""
        if character_count > 0:
            key = (self.tts_config.provider, self.tts_config.model, "tts", "characters")
            self._usage_stats[key] += character_count
    
    def add_stt_seconds(self, audio_duration: float) -> None:
        """Add STT audio duration"""
        if audio_duration > 0:
            key = (self.stt_config.provider, self.stt_config.model, "stt", "seconds")
            self._usage_stats[key] += audio_duration
    
    def add_telephony_seconds(self, duration: float, provider: str = "vonage") -> None:
        """Add telephony duration"""
        if duration > 0:
            key = (provider, "sip", "telephony", "seconds")
            self._usage_stats[key] += duration
    
    def add_vm_infrastructure_seconds(self, duration: float, provider: str = "digitalocean") -> None:
        """Add VM infrastructure usage"""
        if duration > 0:
            key = (provider, "*", "vm_infrastructure", "seconds")
            self._usage_stats[key] += duration
    
    # =========================================================================
    # LIVEKIT EVENT HANDLERS
    # =========================================================================
    
    def on_metrics_collected(self, ev: Any) -> None:
        """Handle LiveKit metrics_collected event"""
        try:
            metric = getattr(ev, 'metrics', ev)
            metric_type = type(metric).__name__
            
            if metric_type == 'STTMetrics':
                audio_duration = getattr(metric, 'audio_duration', 0)
                if audio_duration > 0:
                    self.add_stt_seconds(audio_duration)
            
            elif metric_type == 'LLMMetrics':
                prompt_tokens = getattr(metric, 'prompt_tokens', 0)
                completion_tokens = getattr(metric, 'completion_tokens', 0)
                if prompt_tokens > 0 or completion_tokens > 0:
                    self.add_llm_tokens(prompt_tokens, completion_tokens)
            
            elif metric_type == 'RealtimeModelMetrics':
                input_tokens = getattr(metric, 'input_tokens', 0)
                output_tokens = getattr(metric, 'output_tokens', 0)
                if input_tokens > 0 or output_tokens > 0:
                    self.add_llm_tokens(input_tokens, output_tokens)
            
            elif metric_type == 'TTSMetrics':
                characters = getattr(metric, 'characters_count', 0)
                if characters > 0:
                    self.add_tts_characters(characters)
                    
        except Exception as e:
            logger.warning(f"UsageCollector: Error processing metrics event: {e}")
    
    def on_agent_speech_committed(self, msg: Any) -> None:
        """Handle agent_speech_committed event for TTS character counting"""
        try:
            text = ""
            if hasattr(msg, 'content'):
                text = msg.content or ""
            elif hasattr(msg, 'text'):
                text = msg.text or ""
            elif isinstance(msg, str):
                text = msg
            
            if text:
                self.add_tts_characters(len(text))
                
        except Exception as e:
            logger.warning(f"UsageCollector: Error processing speech committed event: {e}")
    
    # =========================================================================
    # OUTPUT METHODS
    # =========================================================================
    
    def get_summary(self) -> list[dict[str, Any]]:
        """Get usage summary as list of records"""
        results = []
        for (provider, model, component, unit), amount in self._usage_stats.items():
            if amount > 0:
                results.append({
                    "provider": provider,
                    "model": model,
                    "component": component,
                    "unit": unit,
                    "amount": round(amount, 6),
                })
        return results
    
    def get_total_by_component(self) -> dict[str, float]:
        """Get totals grouped by component type"""
        totals: dict[str, float] = defaultdict(float)
        for (_, _, component, _), amount in self._usage_stats.items():
            totals[component] += amount
        return dict(totals)
    
    def is_empty(self) -> bool:
        """Check if any usage has been recorded"""
        return len(self._usage_stats) == 0
    
    def clear(self) -> None:
        """Clear all accumulated usage data"""
        self._usage_stats.clear()
    
    def __repr__(self) -> str:
        summary = self.get_summary()
        return f"UsageCollector(records={len(summary)}, data={summary})"


def attach_usage_collector(session: AgentSession, collector: UsageCollector) -> None:
    """Attach usage collector event handlers to an AgentSession"""
    if not is_component_tracking_enabled():
        logger.debug("Component cost tracking disabled, skipping collector attachment")
        return
    
    try:
        @session.on("metrics_collected")
        def _on_metrics(ev):
            collector.on_metrics_collected(ev)
        
        @session.on("agent_speech_committed") 
        def _on_speech(msg):
            collector.on_agent_speech_committed(msg)
        
        logger.info("UsageCollector attached to AgentSession")
        
    except Exception as e:
        logger.warning(f"Failed to attach UsageCollector to session: {e}")


# =============================================================================
# PRICING CALCULATION
# =============================================================================

@dataclass
class PricingRate:
    """Pricing rate for a component"""
    component: str
    provider: str
    model: str
    unit: str
    cost_per_unit: Decimal


async def calculate_call_cost(
    usage_records: list[dict[str, Any]],
    pricing_rates: list[PricingRate] | None = None,
    db_connection = None,
) -> tuple[Decimal, list[dict[str, Any]]]:
    """Calculate total call cost from usage records"""
    if not usage_records:
        return Decimal("0"), []
    
    if pricing_rates is None and db_connection:
        pricing_rates = await _fetch_pricing_rates(db_connection)
    
    if not pricing_rates:
        logger.warning("No pricing rates available, cannot calculate cost")
        return Decimal("0"), []
    
    # Build lookup dict (lowercase keys for case-insensitive matching)
    rate_lookup: dict[tuple[str, str, str], Any] = {}
    for rate in pricing_rates:
        if isinstance(rate, dict):
            key = (rate["component"].lower(), rate["provider"].lower(), rate["model"].lower())
            rate_lookup[key] = rate
            fallback_key = (rate["component"].lower(), rate["provider"].lower(), "*")
            if fallback_key not in rate_lookup:
                rate_lookup[fallback_key] = rate
        else:
            key = (rate.component.lower(), rate.provider.lower(), rate.model.lower())
            rate_lookup[key] = rate
            fallback_key = (rate.component.lower(), rate.provider.lower(), "*")
            if fallback_key not in rate_lookup:
                rate_lookup[fallback_key] = rate
    
    total_cost = Decimal("0")
    cost_breakdown = []
    
    for record in usage_records:
        component = record["component"]
        provider = record["provider"]
        model = record["model"]
        amount = Decimal(str(record["amount"]))
        
        # Lowercase for case-insensitive lookup
        rate = rate_lookup.get((component.lower(), provider.lower(), model.lower()))
        if not rate:
            rate = rate_lookup.get((component.lower(), provider.lower(), "*"))
        
        if rate:
            if isinstance(rate, dict):
                cost_per_unit = Decimal(str(rate["cost_per_unit"]))
            else:
                cost_per_unit = rate.cost_per_unit
                
            item_cost = amount * cost_per_unit
            total_cost += item_cost
            cost_breakdown.append({
                "component": component,
                "provider": provider,
                "model": model,
                "unit": record["unit"],
                "amount": float(amount),
                "rate": float(cost_per_unit),
                "cost": float(item_cost),
            })
        else:
            logger.warning(f"No pricing rate found for {component}/{provider}/{model}")
            cost_breakdown.append({
                "component": component,
                "provider": provider,
                "model": model,
                "unit": record["unit"],
                "amount": float(amount),
                "rate": None,
                "cost": 0,
            })
    
    cost_breakdown.sort(key=lambda x: (x['component'], x['provider']))
    return total_cost, cost_breakdown


async def _fetch_pricing_rates(db_connection) -> list[PricingRate]:
    """Fetch pricing rates from database"""
    try:
        cursor = db_connection.cursor()
        # Use billing_pricing_catalog table
        # Column mappings: category = component, unit_price = cost_per_unit
        cursor.execute(f"""
            SELECT category, provider, model, unit, unit_price
            FROM {BILLING_PRICING_CATALOG_FULL}
            WHERE is_active = TRUE
        """)
        rows = cursor.fetchall()
        cursor.close()
        
        return [
            PricingRate(
                component=row[0],  # category maps to component
                provider=row[1],
                model=row[2],
                unit=row[3],
                cost_per_unit=Decimal(str(row[4])),
            )
            for row in rows
        ]
    except Exception as e:
        logger.error(f"Failed to fetch pricing rates: {e}")
        return []


__all__ = [
    "UsageRecord",
    "ComponentConfig",
    "UsageCollector",
    "PricingRate",
    "is_component_tracking_enabled",
    "attach_usage_collector",
    "calculate_call_cost",
]
