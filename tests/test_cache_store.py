import unittest

from modules.cache_store import RESTAPIDB_CACHE, invalidate_cached_values


class CacheStoreTests(unittest.TestCase):
    def tearDown(self) -> None:
        RESTAPIDB_CACHE.clear()

    def test_invalidate_cached_values_matches_profile_scoped_keys(self) -> None:
        RESTAPIDB_CACHE["profile:london:restadmin:rest-services:paths"] = (0, ["/london"])
        RESTAPIDB_CACHE["profile:london:restadmin:users:list"] = (0, ["restadmin"])

        invalidate_cached_values("rest-services:")

        self.assertNotIn("profile:london:restadmin:rest-services:paths", RESTAPIDB_CACHE)
        self.assertIn("profile:london:restadmin:users:list", RESTAPIDB_CACHE)


if __name__ == "__main__":
    unittest.main()
