// Hidden test — evaluator-owned. Verifies NotesViewModel public contract.
//
// IMPORTANT: agents legitimately use Dispatchers.Default (or similar) inside
// the ViewModel — that's idiomatic Kotlin. `runTest` + `advanceUntilIdle`
// only drive the test scheduler, NOT a real dispatcher. We therefore use
// real-time waits with timeouts instead of trying to control the agent's
// coroutine scope from outside.

import data.InMemoryNotesRepository
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.filter
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.firstOrNull
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import viewmodel.NotesViewModel
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNotNull
import kotlin.test.assertNull
import kotlin.test.assertTrue
import kotlin.test.fail

/** Wait up to 2s for [predicate] to hold across uiState emissions. */
private suspend fun NotesViewModel.awaitState(predicate: (Any) -> Boolean): Any =
    withTimeout(2_000) {
        uiState.filter { predicate(it as Any) }.first() as Any
    }

class HiddenViewModelTest {

    @Test
    fun `createNote produces Ready state with the new note`() = runBlocking {
        val vm = NotesViewModel(InMemoryNotesRepository())
        vm.createNote("hello", "world", setOf("greeting"))
        val state = vm.awaitState { it.toString().contains("hello", ignoreCase = true) }
        assertTrue(state.toString().contains("hello", ignoreCase = true))
    }

    @Test
    fun `search filter narrows visible notes`() = runBlocking {
        val repo = InMemoryNotesRepository()
        repo.create("Apple", "fruit", emptySet())
        repo.create("Banana", "fruit", emptySet())
        repo.create("Carrot", "vegetable", emptySet())

        val vm = NotesViewModel(repo)
        // Wait for initial load — Ready state with all 3 visible.
        vm.awaitState { s ->
            val str = s.toString()
            str.contains("Apple") && str.contains("Banana") && str.contains("Carrot")
        }

        vm.onSearchQueryChange("apple")
        // Re-extract filteredNotes (every implementation we've seen calls the
        // field literally `filteredNotes`). If the agent named it differently,
        // we fall back to checking that Banana is "filtered out" textually.
        val state = vm.awaitState { s ->
            val str = s.toString()
            val filteredSection = str.substringAfter("filteredNotes=", "")
                .substringBefore("]", "")
            // Apple visible, Banana NOT visible in the filtered section.
            filteredSection.contains("Apple") && !filteredSection.contains("Banana")
        }
        assertTrue(state.toString().contains("Apple"))
    }

    @Test
    fun `tag filter intersects with search filter`() = runBlocking {
        val repo = InMemoryNotesRepository()
        repo.create("Buy milk", "shopping", setOf("home"))
        repo.create("Buy laptop", "shopping", setOf("work"))
        repo.create("Standup notes", "agenda", setOf("work"))

        val vm = NotesViewModel(repo)
        vm.awaitState { it.toString().contains("Buy milk") && it.toString().contains("Buy laptop") }

        vm.onSearchQueryChange("Buy")
        vm.onTagFilterChange("work")

        val state = vm.awaitState { s ->
            val filtered = s.toString().substringAfter("filteredNotes=", "")
                .substringBefore("]", "")
            filtered.contains("Buy laptop") &&
                !filtered.contains("Buy milk") &&
                !filtered.contains("Standup")
        }
        assertTrue(state.toString().contains("Buy laptop"))
    }

    @Test
    fun `deleteNote removes from repository state`() = runBlocking {
        val repo = InMemoryNotesRepository()
        val n = repo.create("doomed", "x", emptySet())
        val vm = NotesViewModel(repo)
        vm.awaitState { it.toString().contains("doomed") }

        vm.deleteNote(n.id)

        // Wait up to 2s for the deletion to land in the repo (VM dispatches async).
        withTimeout(2_000) {
            while (repo.get(n.id) != null) delay(20)
        }
        assertNull(repo.get(n.id))
    }

    @Test
    fun `selectNote with null deselects without error`() = runBlocking {
        val repo = InMemoryNotesRepository()
        val n = repo.create("a", "b", emptySet())
        val vm = NotesViewModel(repo)
        vm.awaitState { it.toString().contains("Ready") || it.toString().contains("a") }

        vm.selectNote(n.id)
        delay(100)
        vm.selectNote(null)
        delay(100)

        // No exception thrown = pass; state remains reachable.
        val s = vm.uiState.firstOrNull()
        assertNotNull(s)
    }
}
