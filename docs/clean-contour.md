# Clean contour

A clean contour is the part of a system in which every entity with a lifecycle
has a declared state, written by a single writer and never derived from
indirect signs, and the boundary of that part is guarded by an instrument.
Everything else is a dirty contour. This rule is for whoever writes or changes
an entity with a lifecycle (an attempt, a build, an order, a run, an
assignment, a payment) or moves a path from the dirty contour into the clean
one.

## Principles

**1. State is a stored field.** The list of values and the table of allowed
transitions are declared once in code; the database constraint is built from
the same list. State is not derived from an event log, from the presence of
rows, from the statuses of neighboring tables, or from another field of the
same entity. The API returns the stored state, and the interface draws by it.

**2. One function writes.** It changes the field in one transaction together
with the transition data and refuses a transition that is not in the table.
Every transition has a named owner.
A table the write function does not read is the same as no table: a manual
"from which status" check in each branch diverges from the table silently. A
test walks through all pairs of statuses and requires the writer to allow
exactly the pairs in the table.

**3. Every wait has a failure state and a way out.** Model, network, queue,
background handler: refusal and timeout are declared transitions. An object
never stays without a next action.

**4. Every transition is covered by a test.**

**5. The entity's code lives in one package.** Writing its state from outside
is forbidden by the import contract. Where the contract cannot forbid a name (a
function inside an allowed module), a guard test stands on that name.

**6. A refusal is recorded with its own reason.** A refusal code is a member of
one list, not a string literal written in place. Different reasons are
different codes: a timeout stays a timeout and does not become a generic
"error", and a catch of all exceptions does not substitute one common code. A
decision is not made by the text of an error message or by a code prefix.

**7. Unknown is not an outcome.** An unknown value, a failed write, a lost
lease, a broken lineage is a refusal, not a silent success.

**8. Parts are accepted separately.** A defect in one part does not fail the
whole if the rest can be accepted. Whether a part is required is set by its
type and by the plan, not by the place where a check happened to fail.

**9. A job kind does not know about other kinds.** Kinds and plans are declared
as data; there are no negative checks of the form "am I not another kind".

**10. The new contour does not call the old one, and the old one does not call
the new one.** A bridge between them is a named exception in the import
contract, and each one is removed together with the old path.

**11. A green check has been shown red.** Every new test and scenario fails at
least once on a real defect: the change is reverted or a defect is planted, and
the check must fail. A check measures the visible result, not a sign that the
code works: the color of the border in focus, not an internal variable of a
library; the answer to a question, not a 200 code. A scenario that passes
thanks to the state of the environment (a lagging index, data from a previous
run, a cache) proves nothing, so it is also run on a clean environment.


**12. Kind is a stored field.** What kind of object this is, what role a column
has, whether a variant is a service one, whether the text has a space is a
`kind`/`role`/flag field declared as a list. Kind is not guessed from a label,
text, an id prefix, a unit of measure, or the shape of a string.

**13. The screen draws what the server returned.** The state of an entity that
is stored on the server arrives as one response field. The browser does not
assemble it from flags, timers, effects, and its own storage.

**14. The outcome does not depend on the order of responses.** If a request
response writes into fields that the user can also change, the fields are
locked until the response or a precedence rule is declared. The speed of an
external system does not change the result.

**15. The server decides what is allowed.** A rule that allows or forbids a
user action (a repeat, a deadline, a limit, access) is checked by the server.
The browser keeps no copy of the rule: it learns what is available from the
server response and shows a refusal by its refusal code.

**16. No value is a refusal, not a default.** A default value is declared once,
in the schema where the field is declared, and is substituted only when the
user did not set a value. Code that reads a value does not substitute its own
default: if the value was not loaded from the database, did not arrive in the
response, or did not parse, that is a refusal with a reason (principle 7).
Forbidden at the place of reading: `.get(key, default)`, `x or default`,
`getattr(object, name, default)`, `except …: return default`, `None` instead of
a value for which the code takes a default. A deferred database column read
without loading raises an error and does not return empty. A test stub of an
object carries the field and does not rely on the code skipping it.

**17. A refusal reaches the user with its own reason.** Every refusal code the
server can return to a user, in a request response or in a stored failure
field, is a member of a list declared in the API contract. The screen shows
every code with its own text; the "code → text" table is built by the type of
the list from the contract, and a code without text is a build error. The
client keeps no lists of codes of its own. Client and server on the same
contour work by one contract.

## How to tell that a contour is dirty

One sign found is already a divergence.

The signs are worked out as cards in the skill `clean-contour-check`
([skills/clean-contour-check/SKILL.md](skills/clean-contour-check/SKILL.md)):
each has a way to search for candidates, closed questions about a place in the
code, what does not count, and a dirty and a clean example. The cards are
applied to one place at a time, so a weak model checks them too: a sign is
admitted to a weak model if, on a corpus of labeled places, it finds at least
90% of the dirty ones and is wrong on no more than 10% of the clean ones.

When to open the cards: before fixing a bug, over the file you are changing;
when closing a task, over your own diff; on the owner's command "hygiene
<area>", over the area: a machine finds candidates, a weak model filters them,
and a strong model works through the design for what remains.

A search hit is a candidate, not a verdict: a line in a test or in the writer
itself is discarded on reading. Signs that can be caught only by reading are
set as questions to every PR, asked by a model over the diff or by a reviewer;
such a check comments and does not block the merge.

Two more signs that the design is wrong, not a line: the same thing was not
fixed in three attempts; every fix opens the next wall. That is a reason to
convert the entity, not to make a fourth patch.

## Instruments

The minimum without which a contour does not count as clean:

- **an import contract** for each boundary from principles 5 and 10, in CI on
  every PR; a guard on top of it catches what the import graph does not see:
  a relative import, an import inside a function, loading by name;
- **a registry of state fields**: a test walks the models, finds every field
  named `status`/`state`/`stage`/`phase`/`outcome`, and requires it to be in
  one of the lists: "declared" (list + transition table), "not a lifecycle"
  (with a reason), "debt"; the debt list only shrinks; the same test checks the
  database constraint against the list;
- **a transition guard** in the write function: in tests it refuses; in the
  working contour, for a table derived by reading the code and not proven by
  tests, it first writes an undeclared transition to the log and lets it
  through, an alarm stands on that log line; once the log is empty the entity
  is switched to refusal;
- **a borrowing guard** during a move: it catches pieces of old code in the new
  one, including reformatted ones;
- **a registry of refusal codes** (principle 17): a server test finds every
  refusal that goes to a user and requires a code from the contract's list; the
  debt list only shrinks; on the client the "code → text" table is typed by the
  list from the contract, and the compiler checks completeness.

Each instrument has a control case on which it is required to fail: a planted
import, a planted field, a planted call of the old function, a member of the
code list without text.

## How to move an entity into the clean contour

Behavior does not change during the move. A strange existing transition is
declared as it is and named in the PR.

1. Read the requirements for the entity's states in the spec. A state that is
   not there is a question, not a decision.
2. Make a map: every read and every write of the state, and where it comes
   from.
3. Declare the list, the transition table, the database constraint and the
   write function; add the field to "declared".
4. Give a failure state to every wait.
5. Move the data: a migration gives a stored value to every object, including
   old ones; folding the old sign is done once, in a transfer module. The
   migration is rehearsed on a copy of the working database.
6. Switch the reads: branches, response codes and writes take input only from
   the stored field, with no fallback path through the old sign.
7. Delete the old computation. "Just in case" lives in git.
8. Close the boundary: a package, an import contract, a guard on the names of
   the old functions.
9. A test for every transition.
10. Independent check: a separate executor without the author's conclusions
    receives a sample of a clean entity and the code of the converted one, only
    reads, and reports violations of the principles with file and line, in
    descending order of consequence for the user. What is found is fixed before
    the task is closed.
11. Turn on the guard's refusal once the log of undeclared transitions for the
    entity is empty.

## How to move a whole path

A path (a user scenario, a vertical) is moved when in the dirty contour it is
fixed point by point and the number of failure places hardly decreases.

1. The unit of the move is the whole path, not a file.
2. The behavior of the old path is written as cards: input, output, refusals.
   What is not in a card the new path does not do; a gap is a defect of the
   card.
3. The lists of kinds, refusals, states and the run plan are declared as data
   before the new code.
4. The new path is written from the cards without reading the old code, in a
   separate session or by a separate executor.
5. The new and old paths are compared on one corpus of recorded answers of
   external systems, then once on live ones.
6. The switch is a flag or a route prefix; the rollback is removing it, without
   deploying code.
7. Monitoring instruments move with the path: each alarm fires on a planted
   state of the new path and goes out after it is removed.
8. The old path is deleted. The move is not closed until the old one is
   deleted.

## When an entity is clean

- the field is in "declared", the debt list has not grown;
- a search by the signs does not find its code;
- an import contract and a guard exist, each failed on a planted violation;
- every transition and every failure state is covered by a test;
- the independent check found no violations;
- the log of undeclared transitions is empty, the guard is in refusal mode;
- the old computation is not in the code.
