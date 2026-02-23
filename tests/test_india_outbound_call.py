"""
Test Real Outbound Call with India LiveKit Credentials

This test demonstrates the complete outbound call flow using:
- India LiveKit server (lk.techiemaya.com)
- Agent ID 33
- Phone number +919513456728 (has livekit_config UUID in database)
- Dynamic credential resolution from database

The test shows:
1. Call routing validation (from_number -> carrier -> trunk_id)
2. LiveKit credential resolution (database -> environment fallback)
3. Agent dispatch with proper metadata
4. Full logging of the dispatch flow

IMPORTANT: This makes a REAL outbound call. Use a test number!
"""

import asyncio
import os
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from api.services.call_service import get_call_service
from utils.call_routing import validate_and_format_call
from utils.livekit_resolver import resolve_livekit_credentials
from db.db_config import get_db_config


async def test_india_outbound_call():
    """
    Test making a real outbound call using India LiveKit credentials.
    
    Configuration:
    - From: +919513456728 (India number with livekit_config in DB)
    - To: Test number (India format)
    - Agent: 33
    - LiveKit: India Cloud (lk.techiemaya.com)
    - Trunk: ST_Li8PtVubMy5u (from rules)
    """
    print("="*80)
    print("REAL OUTBOUND CALL TEST - India LiveKit Cloud")
    print("="*80)
    
    # Configuration
    from_number = "+19513456728"  # India number with livekit_config UUID
    to_number = "+918384884150"  # Test number in India
    agent_id = 33
    tenant_id = "e0a3e9ca-3f46-4bb0-ac10-a91b5c1d20b5"  # Specific tenant for this test
    voice_id = "default"  # Will resolve from agent's default voice
    initiated_by = None  # No user context for this test
    added_context = "This is a test call from the India LiveKit cloud server."
    lead_name = "Test Lead India"
    
    print(f"\n1. Call Configuration:")
    print(f"   From: {from_number}")
    print(f"   To: {to_number}")
    print(f"   Agent ID: {agent_id}")
    print(f"   Tenant ID: {tenant_id}")
    print(f"   Voice: {voice_id}")
    print(f"   Context: {added_context}")
    
    # Step 1: Initialize call service
    print(f"\n2. Initializing Call Service...")
    call_service = get_call_service()
    await call_service._ensure_storage()
    print(f"   ✓ Call service initialized")
    
    # Step 2: Resolve voice
    print(f"\n3. Resolving Voice...")
    try:
        resolved_voice_id, voice_context = await call_service.resolve_voice(
            voice_id, agent_id
        )
        print(f"   ✓ Voice resolved:")
        print(f"     - Voice ID: {resolved_voice_id}")
        print(f"     - TTS Voice ID: {voice_context.tts_voice_id}")
        print(f"     - Provider: {voice_context.provider}")
        print(f"     - Voice Name: {voice_context.voice_name}")
        print(f"     - Is Realtime: {voice_context.is_realtime}")
    except ValueError as e:
        print(f"   ✗ Voice resolution failed: {e}")
        return
    
    # Step 3: Use the specified tenant_id (not from agent)
    print(f"\n4. Using Specified Tenant ID...")
    print(f"   ✓ Tenant ID: {tenant_id[:8]}...")
    
    # Step 4: Validate call routing
    print(f"\n5. Validating Call Routing...")
    routing_result = validate_and_format_call(
        from_number=from_number,
        to_number=to_number,
        db_config=get_db_config(),
        tenant_id=tenant_id,
    )
    
    if not routing_result.success:
        print(f"   ✗ Call routing validation failed: {routing_result.error_message}")
        return
    
    print(f"   ✓ Call routing validated:")
    print(f"     - Original number: {to_number}")
    print(f"     - Formatted number: {routing_result.formatted_to_number}")
    print(f"     - Carrier: {routing_result.carrier_name}")
    print(f"     - Trunk ID (from rules): {routing_result.outbound_trunk_id}")
    print(f"     - LiveKit Config ID: {routing_result.livekit_config_id}")
    
    # Step 5: Resolve LiveKit credentials
    print(f"\n6. Resolving LiveKit Credentials...")
    livekit_creds = await resolve_livekit_credentials(
        from_number=from_number,
        tenant_id=tenant_id,
        routing_result=routing_result,
    )
    
    print(f"   ✓ LiveKit credentials resolved:")
    print(f"     - Source: {livekit_creds.source}")
    print(f"     - URL: {livekit_creds.url}")
    print(f"     - API Key: {livekit_creds.api_key}")
    print(f"     - API Secret: {livekit_creds.api_secret[:20]}... (decrypted)")
    print(f"     - Trunk ID: {livekit_creds.trunk_id}")
    print(f"     - Worker Name: {livekit_creds.worker_name}")
    
    # Verify we're using India LiveKit credentials
    expected_url = "wss://lk.techiemaya.com"
    expected_api_key = "API5QH2NJHDXQSW"
    expected_trunk = "ST_MmVqEuBMDNf6"
    expected_worker = "voag-dev"
    
    if livekit_creds.source == "database":
        if livekit_creds.url == expected_url:
            print(f"   ✓ Using India LiveKit URL (correct)")
        else:
            print(f"   ✗ URL mismatch: expected {expected_url}, got {livekit_creds.url}")
        
        if livekit_creds.api_key == expected_api_key:
            print(f"   ✓ Using India API key (correct)")
        else:
            print(f"   ✗ API key mismatch")
        
        if livekit_creds.trunk_id == expected_trunk:
            print(f"   ✓ Using correct trunk ID")
        else:
            print(f"   ✗ Trunk ID mismatch: expected {expected_trunk}, got {livekit_creds.trunk_id}")
        
        if livekit_creds.worker_name == expected_worker:
            print(f"   ✓ Using correct worker name")
        else:
            print(f"   ✗ Worker name mismatch: expected {expected_worker}, got {livekit_creds.worker_name}")
    else:
        print(f"   ⚠ Using environment credentials (not database)")
        print(f"     This means the feature flag might be disabled or config not found")
    
    # Step 6: Dispatch the call
    print(f"\n7. Dispatching Call...")
    print(f"   ⚠ WARNING: This will make a REAL outbound call to {to_number}")
    print(f"   Press Ctrl+C within 5 seconds to cancel...")
    
    try:
        await asyncio.sleep(5)
    except KeyboardInterrupt:
        print(f"\n   ✗ Call cancelled by user")
        return
    
    try:
        import uuid
        job_id = uuid.uuid4().hex
        
        result = await call_service.dispatch_call(
            job_id=job_id,
            voice_id=resolved_voice_id,
            voice_context=voice_context,
            from_number=from_number,
            to_number=to_number,
            context=added_context,
            initiated_by=initiated_by,
            agent_id=agent_id,
            llm_provider=None,
            llm_model=None,
            knowledge_base_store_ids=None,
            lead_name=lead_name,
            lead_id_override=None,
        )
        
        if result.error:
            print(f"   ✗ Dispatch failed: {result.error}")
            return
        
        print(f"   ✓ Call dispatched successfully!")
        print(f"     - Room Name: {result.room_name}")
        print(f"     - Dispatch ID: {result.dispatch_id}")
        print(f"     - Call Log ID: {result.call_log_id}")
        print(f"     - Lead ID: {result.lead_id}")
        print(f"     - Lead Name: {result.lead_name}")
        
    except Exception as e:
        print(f"   ✗ Dispatch failed with exception: {e}")
        import traceback
        traceback.print_exc()
        return
    
    # Step 7: Summary
    print(f"\n8. Test Summary:")
    print(f"   ✓ Call successfully dispatched to LiveKit")
    print(f"   ✓ Using India LiveKit server: {livekit_creds.url}")
    print(f"   ✓ Trunk ID: {livekit_creds.trunk_id}")
    print(f"   ✓ Worker Name: {livekit_creds.worker_name}")
    print(f"   ✓ Agent ID: {agent_id}")
    print(f"   ✓ Call Log ID: {result.call_log_id}")
    print(f"\n   The call should now be connecting...")
    print(f"   Check the LiveKit dashboard or call logs for status.")
    print(f"   Room name: {result.room_name}")
    
    print("\n" + "="*80)
    print("TEST COMPLETE")
    print("="*80)


if __name__ == "__main__":
    # Check environment
    print("\nEnvironment Check:")
    print(f"  - USE_SELFHOST_ROUTING_TABLE: {os.getenv('USE_SELFHOST_ROUTING_TABLE', 'true')}")
    print(f"  - DB_HOST: {os.getenv('DB_HOST', 'not set')}")
    print(f"  - DB_NAME: {os.getenv('DB_NAME', 'not set')}")
    
    # Run test
    asyncio.run(test_india_outbound_call())
