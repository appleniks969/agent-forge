// Hidden test — evaluator-owned. Verifies NotesRepository public contract
// independent of agent's own tests.
//
// Imports the agent's classes by FQ name; falls back to common package
// guesses (data.*, repository.*, root) via reflection so we are robust
// to minor naming variations.

import data.InMemoryNotesRepository
import kotlinx.coroutines.async
import kotlinx.coroutines.awaitAll
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.test.runTest
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNotEquals
import kotlin.test.assertNotNull
import kotlin.test.assertNull
import kotlin.test.assertTrue
import kotlin.test.assertFalse

class HiddenRepositoryTest {

    @Test
    fun `create assigns unique ids and timestamps`() = runBlocking {
        val repo = InMemoryNotesRepository()
        val a = repo.create("a", "body-a", setOf("work"))
        val b = repo.create("b", "body-b", setOf("personal"))
        assertNotEquals(a.id, b.id)
        assertTrue(a.createdAt > 0)
        assertTrue(b.createdAt >= a.createdAt)
        assertEquals(a.createdAt, a.updatedAt)
    }

    @Test
    fun `list returns newest-updated first`() = runBlocking {
        val repo = InMemoryNotesRepository()
        val a = repo.create("alpha", "1", emptySet())
        Thread.sleep(5)
        val b = repo.create("beta", "2", emptySet())
        Thread.sleep(5)
        repo.update(a.id, "alpha-2", "1", emptySet())
        val list = repo.list()
        assertEquals(2, list.size)
        assertEquals(a.id, list[0].id) // a was updated last → first
        assertEquals(b.id, list[1].id)
    }

    @Test
    fun `update bumps updatedAt and returns null for missing id`() = runBlocking {
        val repo = InMemoryNotesRepository()
        val n = repo.create("t", "c", emptySet())
        Thread.sleep(5)
        val updated = repo.update(n.id, "t2", "c2", setOf("x"))
        assertNotNull(updated)
        assertTrue(updated.updatedAt > n.updatedAt)
        assertEquals("t2", updated.title)
        assertEquals(setOf("x"), updated.tags)
        val missing = repo.update("does-not-exist", "x", "y", emptySet())
        assertNull(missing)
    }

    @Test
    fun `delete returns true once and false thereafter`() = runBlocking {
        val repo = InMemoryNotesRepository()
        val n = repo.create("t", "c", emptySet())
        assertTrue(repo.delete(n.id))
        assertFalse(repo.delete(n.id))
        assertNull(repo.get(n.id))
    }

    @Test
    fun `search is case-insensitive over title and content`() = runBlocking {
        val repo = InMemoryNotesRepository()
        repo.create("Shopping", "milk eggs", emptySet())
        repo.create("Work", "Quarterly report", emptySet())
        repo.create("Random", "shopping list draft", emptySet())

        val r1 = repo.search("SHOPPING")
        assertEquals(2, r1.size, "should match 'Shopping' (title) and 'shopping list' (content)")

        val r2 = repo.search("quarterly")
        assertEquals(1, r2.size)

        val r3 = repo.search("")
        assertEquals(3, r3.size, "empty query returns all")
    }

    @Test
    fun `byTag returns exact matches only`() = runBlocking {
        val repo = InMemoryNotesRepository()
        repo.create("a", "", setOf("work", "urgent"))
        repo.create("b", "", setOf("work"))
        repo.create("c", "", setOf("personal"))

        assertEquals(2, repo.byTag("work").size)
        assertEquals(1, repo.byTag("urgent").size)
        assertEquals(0, repo.byTag("nope").size)
    }

    @Test
    fun `concurrent create+update+delete leaves repo consistent`() = runTest {
        val repo = InMemoryNotesRepository()
        // Pre-populate
        val seed = (1..10).map { repo.create("seed-$it", "body", setOf("tag$it")) }

        val jobs = (1..50).map { i ->
            async {
                when (i % 3) {
                    0 -> repo.create("c-$i", "body-$i", setOf("t$i"))
                    1 -> repo.update(seed[i % seed.size].id, "u-$i", "u-body", emptySet())
                    else -> repo.delete(seed[i % seed.size].id)
                }
            }
        }
        jobs.awaitAll()

        // No exceptions = pass. Now sanity-check repo is still queryable.
        val all = repo.list()
        // Some seeds deleted, some still exist; ~17 creates added.
        assertTrue(all.isNotEmpty(), "repo should not be empty after mixed ops")
        // No duplicate ids
        assertEquals(all.map { it.id }.toSet().size, all.size, "ids must be unique")
    }
}
