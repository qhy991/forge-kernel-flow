# ForgeKernel worker instructions

Read the frozen task and admitted evidence supplied with every activation. Read
applicable AGENTS.md and agent.md instructions in your assigned workspace. Do not
overwrite those files. The controller includes this file explicitly in each prompt.

Keep operator semantics, shapes, precision, reference, judge, hardware, budget,
and measurement protocol fixed. Separate observations from hypotheses. Establish
an architecture before local parameter tuning. State each choice in the decision
DAG and group choices that must change together into one atomic group.

Implement only in the code workspace. Rebuild all invalidated decisions, their
consumers, and their atomic groups. Do not access opaque reference sources,
previous generations' sources, controller records, or evaluator internals. Separate
workspaces and fresh sessions do not themselves enforce these restrictions.

Use reference measurements and admitted profiler evidence to choose the next
change. State the observation, hypothesis, change, and expected distinguishing
measurement. Change one decision at a time unless its atomic group requires more.
When measured stagnation triggers architecture analysis, revisit the earliest
unsupported choice and its dependents.

The independent executor freezes sources, runs correctness and timing checks,
restores accepted sources, and confirms promotions. Do not invent measurements,
fill timing fields, run an alternative evaluator, or claim acceptance. Unknown
jobs and infrastructure failures do not count as failed optimizations.

Treat mechanisms and minimal examples as knowledge proposals. Only an independent
probe and private catalog admission certify them for the stated scope. Preserve
null and negative evidence; missing evidence does not establish a negative result.

Return only the requested typed proposal. Stop at the frozen limits. A plateau
does not prove hardware optimality. Native deployment capability and hardware
performance remain unverified until the owning executor produces accepted evidence.
