import asyncio
import unittest
from unittest.mock import MagicMock, AsyncMock
from dataclasses import asdict
from refactor import BusPlannerService

# Paste the REFACTORED code below this block to run the test, 
# or import the refactored class. 
# For this output, we assume the code below is available in namespace.

async def test_refactoring_equivalence():
    """
    Verifies that the Refactored BusPlannerService logic produces 
    the same data structure as expected given specific mocked inputs.
    """
    
    # 1. Setup Mocks
    mock_repo = MagicMock()
    mock_cache = MagicMock()
    mock_static = MagicMock()
    
    # Mock Repository Data
    mock_repo.get_sids_by_name.side_effect = lambda name: ["sid_A"] if name == "Start" else ["sid_B"]
    mock_repo.get_info.return_value = {"name": "Start", "slid": "slid_123", "sid": "sid_A"}
    mock_repo.get_geo.return_value = None # Simplify for test
    
    # Mock Static Route Matching
    # A route that goes Start(sid_A) -> End(sid_B)
    mock_static.find_routes_between.return_value = [{
        'route_name': '307',
        'rid': 'rid_307',
        'stops_sid': ['sid_A', 'sid_B'],
        'match_range': (0, 1),
        'direction_text': 'East'
    }]
    
    # Mock Cache (Miss)
    mock_cache.get.return_value = None
    
    # Mock Network Client
    mock_client_session = AsyncMock() # Not used directly due to dependency injection in main
    
    # We need to mock the internal methods that do network calls if we don't want to run full async network tests
    # However, to test logic flow, we will inspect the Service logic.
    
    service = BusPlannerService(db_file=MagicMock(), cache_file=MagicMock(), static_route_file=MagicMock())
    service.repo = mock_repo
    service.cache = mock_cache
    service.static_routes = mock_static
    
    # Mock the realtime fetcher to avoid HTTP calls
    # This simulates: fetch_realtime_by_slid returns a bus on rid_307 arriving in 5 mins
    async def mock_fetch_realtime(client, slid, rep_sid):
        return [{
            'route': '307',
            'rid': 'rid_307',
            'sid': rep_sid,
            'time_text': '5分',
            'raw_time': 300
        }]
    
    service.fetch_realtime_by_slid = mock_fetch_realtime

    # 2. Execution
    results = await service.plan_route("Start", "End")

    # 3. Assertions
    print("Testing Refactored Logic...")
    assert len(results) == 1, f"Expected 1 bus, got {len(results)}"
    bus = results[0]
    
    assert bus.route_name == "307"
    assert bus.rid == "rid_307"
    assert bus.arrival_time_text == "5分"
    assert bus.stop_count == 1 # 2 stops total, so 1 hop
    
    print("✅ Logic Verification Passed: Route planning flow is intact.")

if __name__ == "__main__":
    # To run this, one would need the refactored class definition above.
    # This is a template for the user to verify.
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(test_refactoring_equivalence())
    except Exception as e:
        print(f"❌ Test Failed: {e}")