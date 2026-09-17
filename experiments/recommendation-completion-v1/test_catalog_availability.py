"""Optional card outages must not relax ownership or provenance validation."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'agent'))
import unittest
from unittest.mock import AsyncMock, patch
from fastapi import HTTPException
from app.catalog_commerce import resolve_optional_cards


class AvailabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_transient_outages_have_no_commerce_authority(self):
        for code in (502, 503, 504):
            with patch('app.catalog_commerce.resolve_cards', AsyncMock(side_effect=HTTPException(code))):
                cards, notice = await resolve_optional_cards({})
                self.assertEqual(cards, [])
                self.assertIn('不提供报价、库存及购买权限', notice)

    async def test_ownership_and_other_errors_still_fail(self):
        for code in (401, 403, 409, 500):
            with patch('app.catalog_commerce.resolve_cards', AsyncMock(side_effect=HTTPException(code))):
                with self.assertRaises(HTTPException):
                    await resolve_optional_cards({})

    async def test_provenance_errors_still_fail(self):
        with patch('app.catalog_commerce.resolve_cards', AsyncMock(side_effect=ValueError('identity_mismatch'))):
            with self.assertRaises(ValueError):
                await resolve_optional_cards({})

    async def test_success_preserves_verified_cards(self):
        cards = [{'id': '42', 'sourceDocid': 'kuaisearch:123'}]
        with patch('app.catalog_commerce.resolve_cards', AsyncMock(return_value=cards)):
            self.assertEqual(await resolve_optional_cards({}), (cards, None))


if __name__ == '__main__':
    unittest.main()
