
# xfail

  i
n `unittest`, the equivalent choices are:

**`@unittest.skip`** — unconditional skip (closest to what you want for a slow-passing test):

```python
@unittest.skip("takes 40min")
def test_foo(self): ...
```

**`@unittest.skipUnless`** — opt-in via env var (better for CI control):

```python
@unittest.skipUnless(os.environ.get("RUN_SLOW_TESTS"), "takes 40min, set RUN_SLOW_TESTS=1 to run")
def test_foo(self): ...
```

**`@unittest.expectedFailure`** — only if the test is actually expected to _fail_. If it passes, unittest reports it as `UNEXPECTED_SUCCESS` (that's the `strict=True` analogue). But your comment says the test succeeds — so this is semantically wrong for your case.

For a slow-but-correct test, `@unittest.skipUnless` with an env guard is the idiomatic unittest pattern:

|   |   |   |   |
|---|---|---|---|
|\|   \|<br>\|---\|<br>\|### Johannes Bellert\||Tue, May 26, 10:55 AM (5 days ago)|||
|\|   \|<br>\|---\|<br>\|to me<br><br>![](https://mail.google.com/mail/u/0/images/cleardot.gif)\||   |   |

I have 500 unit test cases in my Python project and 2% are failing because of bugs I have logged in my task list and fixing is due.

Each time I run the entrie test suite, I have to check fit if the test fail is caused by a documented bug or side effect. That raise my question if test cases are good for documenting bugs as I learned from Kent Beck. How to cope?  
>>  
  
Yes—tests are good for documenting bugs, but **failing tests are usually a bad long-term way to document known bugs in the main suite**.

What you are experiencing is the classic cost: once the suite has “expected failures,” every run becomes noisy. You stop trusting red. That weakens the main value of unit tests: **red should mean “something unexpectedly broke.”**

A practical way to cope:

**1. Keep the main test suite trustworthy**

Your default test command should answer one question:

Did I introduce any unexpected regressions?

So the normal suite should ideally be **green**. If 2% of 500 tests fail, that is about **10 known failing tests**. Ten failures is enough to create daily friction and make real regressions easier to miss.

**2. Convert known-bug tests into “expected failures”**

In pytest, use xfail for tests that describe a real bug but are not fixed yet:

import pytest

@pytest.mark.xfail(

    reason="BUG-1234: incorrect rounding for negative premiums",

    strict=True,

)

def test_negative_premium_rounding():

    assert calculate_premium(-10.005) == -10.01

Why strict=True matters:

@pytest.mark.xfail(strict=True)

means:

- If the bug is still present, the test is reported as **xfailed**, not as a noisy failure.
- If the test unexpectedly passes, pytest reports it as a failure, telling you: **the bug may have been fixed; remove the xfail.**

That gives you the best of both worlds: the bug is documented as executable behavior, but your normal signal stays clean.

**3. Link every known-bug test to a task**

Do not just write:

@pytest.mark.xfail(reason="known bug")

Write:

@pytest.mark.xfail(reason="JIRA-4711: parser drops timezone on DST boundary", strict=True)

A known failing test without a ticket becomes technical debt with no owner. A known failing test with a ticket becomes an executable reminder.

**4. Separate “regression tests” from “bug reproduction tests”**

A good pattern is:

tests/

  unit/

  integration/

  regression/

  known_bugs/

Or use markers:

@pytest.mark.known_bug

@pytest.mark.xfail(reason="BUG-1234", strict=True)

def test_reproduces_bug_1234():

    ...

Then you can run:

pytest

for normal confidence, and:

pytest -m known_bug

when reviewing known issues.

You can also exclude them from certain runs:

pytest -m "not known_bug"

But I would usually still include xfail tests in CI, because strict=True can tell you when a bug has accidentally been fixed.

**5. When the bug is fixed, turn the xfail into a normal regression test**

The lifecycle should be:

Bug discovered

→ Write a test that reproduces it

→ Mark it xfail with ticket reference

→ Fix the bug

→ Remove xfail

→ Keep the test forever as a regression test

That is probably closest to the Kent Beck lesson: a bug should often lead to a test, but that does **not** mean your suite should remain red until the bug is fixed.

**6. Avoid documenting bugs with broad or fragile tests**

A bug-documenting test should be small and precise.

Bad:

def test_monthly_report_generation():

    ...

Better:

def test_report_uses_policy_timezone_when_created_near_midnight():

    ...

The test should make it obvious what behavior is wrong. Otherwise, when it fails, you still have to investigate whether it is the known bug or a side effect.

**7. Use failure budgets carefully**

Having around 10 known failures may be manageable, but it should not become normal. I would treat known-bug tests as a queue with rules, for example:

- Every xfail must have a ticket.
- Every xfail must have an owner or component.
- Every xfail older than a certain age gets reviewed.
- No xfail without strict=True.
- No vague reason like “flaky” or “known issue.”

**My recommendation**

Use tests to document bugs, yes. But do **not** leave them as ordinary failing tests in your default suite.

Use this discipline:

@pytest.mark.xfail(reason="BUG-1234: documented known issue", strict=True)

Then your suite can stay green while still preserving the executable bug documentation. The important distinction is:

A test documenting a known bug is valuable.  
A permanently red test suite is not.

my unittest use excessively MagicMock and only a few end-to-end integration test. I always doubt if the green of MagicMock tests is telling the full true. Adding more end-to-end integration test comes with the cost such as  increase the dependency of database content etc.

what is the industry practive here?

-->

Yes. In Python’s built-in unittest, the equivalent is:

@unittest.expectedFailure

Example:

import unittest

class PremiumTests(unittest.TestCase):

    @unittest.expectedFailure

    def test_negative_premium_rounding(self):

        self.assertEqual(calculate_premium(-10.005), -10.01)

Behavior:

bug still exists   → reported as expected failure

test passes        → reported as unexpected success

So it is very similar to pytest.mark.xfail.

One limitation: unittest.expectedFailure does **not** have a built-in reason= field like pytest. A common workaround is to put the bug ID in the test name or docstring:

import unittest

class PremiumTests(unittest.TestCase):

    @unittest.expectedFailure

    def test_bug_1234_negative_premium_rounding(self):

        """BUG-1234: incorrect rounding for negative premiums."""

        self.assertEqual(calculate_premium(-10.005), -10.01)

Or use a small custom decorator for readability:

import functools

import unittest

def expected_failure_for(reason):

    def decorator(test_func):

        test_func.__doc__ = reason

        return unittest.expectedFailure(test_func)

    return decorator

Usage:

class PremiumTests(unittest.TestCase):

    @expected_failure_for("BUG-1234: incorrect rounding for negative premiums")

    def test_negative_premium_rounding(self):

        self.assertEqual(calculate_premium(-10.005), -10.01)

Important caveat: in unittest, an “unexpected success” may not fail the whole test run depending on the runner. If you want CI to fail when an expected-failure test starts passing, verify your runner treats unexpected successes as a failure. Many teams add a CI check for that, because it is the signal that the bug may be fixed and the decorator should be removed.

Your doubt is justified. A green suite with excessive MagicMock can mean:

“The code does what my mocks expect,”  
not necessarily  
“The system works.”

That does **not** mean mocks are bad. It means mocks are easy to overuse.

Industry practice is usually a **balanced test pyramid**: many fast unit tests, fewer integration tests, and a small number of end-to-end tests. The test pyramid idea is widely used to describe tests at different granularities and to keep expensive end-to-end tests limited. ([martinfowler.com](https://martinfowler.com/articles/practical-test-pyramid.html?utm_source=chatgpt.com "The Practical Test Pyramid - Martin Fowler")) Google’s testing guidance also stresses that a healthy suite includes medium and large tests because they give higher fidelity than small tests with mocks. ([Google Testing Blog](https://testing.googleblog.com/2024/02/increase-test-fidelity-by-avoiding-mocks.html?utm_source=chatgpt.com "Google Testing Blog: Increase Test Fidelity By Avoiding Mocks"))

**The core principle**

Use mocks for **boundaries**, not for your own internal logic.

Good candidates for mocks:

external HTTP APIs

email sending

message queues

cloud services

payment systems

time / randomness

slow or nondeterministic dependencies

Risky candidates for mocks:

your own repository classes

your own service classes

your own domain objects

your own SQL behavior

your own serialization logic

When you mock your own code too much, you often test implementation details instead of behavior. Then refactoring breaks tests even when behavior is unchanged, or worse, tests stay green while the real collaboration is broken.

**A healthier structure**

For a Python business application, I would usually aim for something like this:

Many unit tests

- pure functions

- domain rules

- validation

- calculations

- edge cases

- no database

- few or no mocks

Moderate integration tests

- real database schema

- real SQL / ORM mapping

- real repository + service interaction

- controlled test data

- no external network

Few end-to-end tests

- happy path

- one or two critical business flows

- smoke tests

- deployment confidence

The missing layer in many projects is not more full end-to-end tests. It is usually **integration tests against real infrastructure in a controlled environment**.

**Do not jump from MagicMock-heavy unit tests straight to full E2E**

Your concern about database content is real. End-to-end tests that depend on whatever happens to be in a shared database become fragile and expensive.

Instead, prefer **hermetic integration tests**:

test creates its own data

test owns its database transaction

test does not depend on pre-existing rows

test can run alone or in any order

test cleans up after itself

For database-heavy code, a good integration test is often:

def test_policy_is_loaded_with_covered_risks(test_db):

    # arrange: insert exact rows needed for this test

    policy_id = insert_policy(test_db, status="ACTIVE")

    insert_risk(test_db, policy_id=policy_id, type="FIRE")

    # act: use real repository / query code

    policy = PolicyRepository(test_db).get(policy_id)

    # assert: check business-visible behavior

    assert policy.status == "ACTIVE"

    assert policy.has_risk("FIRE")

That is much more useful than:

repo = MagicMock()

repo.get.return_value = Policy(...)

because the mock version does not prove your query, mapping, joins, transaction behavior, or schema assumptions are correct.

**A practical rule of thumb**

For each mocked test, ask:

If the real collaborator changed, would this test catch the problem?

If the answer is “no,” the mock is probably reducing confidence.

Example of a low-value mock test:

repo = MagicMock()

service = PolicyService(repo)

service.cancel_policy("P123")

repo.save.assert_called_once()

This mostly tests that the implementation calls save.

Better:

def test_cancel_policy_marks_policy_cancelled():

    policy = Policy(status="ACTIVE")

    service = PolicyService(FakePolicyRepository(policy))

    service.cancel_policy([policy.id](http://policy.id/))

    assert policy.status == "CANCELLED"

Even better at integration level:

def test_cancel_policy_persists_cancelled_status(test_db):

    policy_id = insert_policy(test_db, status="ACTIVE")

    service = build_policy_service_with_real_repository(test_db)

    service.cancel_policy(policy_id)

    assert load_policy_status(test_db, policy_id) == "CANCELLED"

**Prefer fakes over MagicMock for important dependencies**

A fake is a small working implementation used for tests.

Instead of:

repository = MagicMock()

repository.find.return_value = Policy(...)

use:

class FakePolicyRepository:

    def __init__(self):

        self.policies = {}

    def save(self, policy):

        self.policies[[policy.id](http://policy.id/)] = policy

    def find(self, policy_id):

        return self.policies[policy_id]

Fakes are usually better than mocks when the dependency is part of your own system. They make tests less coupled to method calls and more focused on behavior.

**Use MagicMock more safely when you do use it**

Python’s unittest.mock is flexible; MagicMock creates attributes and methods dynamically as they are accessed. That flexibility is useful, but it can also hide mistakes. ([Python documentation](https://docs.python.org/3/library/unittest.mock.html?utm_source=chatgpt.com "unittest.mock — mock object library — Python 3.14.5 documentation"))

Use autospec or spec_set where possible:

from unittest.mock import create_autospec

repo = create_autospec(PolicyRepository, instance=True)

or:

repo = MagicMock(spec_set=PolicyRepository)

This helps catch cases where your test mocks a method that does not actually exist.

Avoid this style when possible:

mock.some.deep.chain.return_value.foo.return_value = 123

Deep mock chains are a smell. They usually mean the code under test knows too much about internal structure.

**How many integration tests should you add?**

Do not aim for a fixed percentage. Aim for **risk coverage**.

Add integration tests around places where mocks commonly lie:

database queries and ORM mappings

transactions and rollback behavior

schema assumptions

serialization/deserialization

configuration loading

authentication/authorization boundaries

message publishing/consuming

file format parsing

external API client contracts

For your 500-test suite, I would not suddenly add 200 end-to-end tests. I would start with perhaps **10–30 targeted integration tests** around the riskiest paths.

Especially add integration tests for:

code that has had production bugs

code with complex SQL

code with complex mocking

code that coordinates several components

code that is business-critical

**What I would do in your situation**

I would classify the current tests into four buckets:

A. Good unit tests

Keep them.

B. Mock-heavy but useful tests

Keep them, but add autospec/spec_set.

C. Mock-heavy tests that only assert internal calls

Gradually rewrite to assert behavior.

D. Missing integration coverage

Add targeted tests with real database setup.

A simple review checklist:

Does this test assert user/business-visible behavior?

Could it pass while the real database query is broken?

Is it mocking code we own?

Would a fake be clearer than a MagicMock?

Is the test coupled to implementation details?

Does an integration test already cover this collaboration?

**Database dependency: the industry answer**

The usual industry approach is **not** “depend on existing database content.”

It is:

use a test database

apply migrations/schema automatically

create test data inside the test

wrap each test in a transaction and roll back

or recreate containers/schemas for test runs

seed only stable reference data

avoid tests depending on run order

Common options:

SQLite in memory

Good for simple persistence tests, but risky if production uses PostgreSQL/Oracle/etc.

Real database in Docker/Testcontainers

Higher fidelity. Slower, but catches real SQL/schema issues.

Dedicated integration-test database

Useful in enterprise environments, but must be isolated and resettable.

Transaction rollback per test

Fast and clean if your stack supports it well.

The most important rule:

Integration tests should depend on data they create, not on data they hope already exists.

**A good target state**

Your suite should tell you different things at different speeds:

Unit tests:

“Do my business rules work?”

Integration tests:

“Do my components work together with real infrastructure?”

End-to-end tests:

“Does the critical user journey work?”

Manual/exploratory testing:

“Did we miss something surprising?”

So the industry practice is not “mock everything” and not “E2E everything.”

It is:

mock at true boundaries, test your own important collaborations with fakes or integration tests, and keep E2E small but meaningful.

For your project, the most valuable next step is probably to pick the **10 most MagicMock-heavy tests around important behavior** and ask: “What real thing are we afraid is broken?” Then add a small integration test for that real risk.