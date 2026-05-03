# Task — Notes app with Jetpack Compose (Multiplatform Desktop)

You are extending a pre-configured Compose Multiplatform Desktop project to
build a small notes application. The Gradle setup, dependencies, and a
placeholder `Main.kt` are already in place. Your job is to add the source
files that implement the spec below.

**Working directory layout (provided):**

```
build.gradle.kts         ← do not modify (toolchain & deps are correct)
settings.gradle.kts      ← do not modify
gradle.properties        ← do not modify
src/main/kotlin/Main.kt  ← keep `fun main()` as the entry point
src/test/kotlin/         ← put your tests here
```

**Verify with:**

```bash
JAVA_HOME=/Users/nikhilsalunke/Library/Java/JavaVirtualMachines/jbr-17.0.14/Contents/Home \
  gradle --no-daemon compileKotlin compileTestKotlin test
```

The first compile takes ~15 s warm; subsequent runs are faster. Iterate
until both `compileKotlin` and `test` pass.

---

## Functional spec

Build a single-window notes app. Users can create, edit, delete, search,
and tag notes. State must survive within a single session (no persistence
to disk required).

### Data model

A `Note` is:
- `id: String` — unique, generated on creation
- `title: String`
- `content: String`
- `tags: Set<String>` — lowercase, no empty strings
- `createdAt: Long` — epoch millis
- `updatedAt: Long` — epoch millis, updated on every edit

### Repository — `NotesRepository` (interface) + `InMemoryNotesRepository`

Required methods:

- `suspend fun list(): List<Note>` — newest-updated first
- `suspend fun get(id: String): Note?`
- `suspend fun create(title: String, content: String, tags: Set<String>): Note`
   — generates id, sets timestamps, returns the new note
- `suspend fun update(id: String, title: String, content: String, tags: Set<String>): Note?`
   — returns updated note, or `null` if id not found; bumps `updatedAt`
- `suspend fun delete(id: String): Boolean` — true if deleted
- `suspend fun search(query: String): List<Note>` — case-insensitive substring
   match on title OR content; empty query returns full list
- `suspend fun byTag(tag: String): List<Note>` — exact tag match (lowercase)

Thread-safe under concurrent suspend calls (use `Mutex` from kotlinx.coroutines).

### ViewModel — `NotesViewModel`

Constructor takes a `NotesRepository`. Exposes:

- `val uiState: StateFlow<NotesUiState>` — observable state for the UI
- `fun onSearchQueryChange(query: String)` — update filter
- `fun onTagFilterChange(tag: String?)` — null clears tag filter
- `fun createNote(title: String, content: String, tags: Set<String>)`
- `fun updateNote(id: String, title: String, content: String, tags: Set<String>)`
- `fun deleteNote(id: String)`
- `fun selectNote(id: String?)` — null deselects

`NotesUiState` (sealed class or data class) must distinguish:
- `Loading` (initial)
- `Ready(notes, filteredNotes, selectedNote, searchQuery, tagFilter, allTags)`
- `Error(message)`

`filteredNotes` reflects the current `searchQuery` and `tagFilter` combined.
`allTags` is the union of tags across all notes, sorted alphabetically.

ViewModel **must NOT** import anything from `androidx.compose.ui.*` or
`androidx.compose.foundation.*` or `androidx.compose.material*`. It is a
pure logic layer over `kotlinx.coroutines.flow.*`.

### UI — Composables

At minimum:

- `NotesApp()` — root Composable; takes a `NotesViewModel`
- `NotesListScreen(...)` — left/main pane: search bar, tag filter chips, list of notes
- `NoteEditorScreen(...)` — right pane (or modal): title field, content field, tags input, save/delete buttons
- `NoteCard(...)` — reusable card item used in the list

Use `@Composable`, `remember`, `mutableStateOf`, `collectAsState` (from
`androidx.compose.runtime.*`). State hoisting expected — leaf composables
take values + lambdas, do not call ViewModel directly.

### Tests (your own)

In `src/test/kotlin/`, write JUnit 5 tests (use `kotlin.test` or `org.junit.jupiter.api`):

- Repository: create, update, delete, list ordering, search (case-insensitive),
  tag filter, concurrent-safety smoke (launch ≥ 20 coroutines doing
  create/update/delete; assert no exceptions and consistent state).
- ViewModel: state transitions for create/update/delete, search filtering,
  combined tag + search filter, `allTags` derivation.

---

## Project structure (required)

Split your code across **at least 5 `.kt` files** in **at least 3 packages**.

**Package names are mandated** (so the evaluator's hidden tests can import
your classes without guessing):

| Layer | Package |
|---|---|
| Domain model | `model` |
| Data / repositories | `data` |
| ViewModels & UI state | `viewmodel` |
| Composables | `ui` (and `ui.components` for shared widgets) |

Required layout (filenames flexible, package declarations are not):

```
src/main/kotlin/
  Main.kt                              ← root package, keep main()
  model/Note.kt                        ← package model
  data/NotesRepository.kt              ← package data
  data/InMemoryNotesRepository.kt      ← package data
  viewmodel/NotesViewModel.kt          ← package viewmodel
  viewmodel/NotesUiState.kt            ← package viewmodel
  ui/NotesApp.kt                       ← package ui
  ui/NotesListScreen.kt                ← package ui
  ui/NoteEditorScreen.kt               ← package ui
  ui/components/NoteCard.kt            ← package ui.components
src/test/kotlin/
  data/NotesRepositoryTest.kt          ← package data
  viewmodel/NotesViewModelTest.kt      ← package viewmodel
```

The class names must also match exactly: `Note`, `NotesRepository`,
`InMemoryNotesRepository`, `NotesViewModel`, `NotesUiState` (sealed class
with `Loading`, `Ready(...)`, `Error(message)` subtypes).

Layering rule: **ui → viewmodel → data → model**, never reversed.

---

## Definition of done

- `gradle compileKotlin compileTestKotlin test` exits 0
- All tests pass (your own + any tests already present)
- `./Main.kt` still has a `main()` function and the app would launch (no need to actually launch)
- Code is split across the required files/packages
- Layering rule above is respected
