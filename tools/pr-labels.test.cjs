const assert = require('node:assert/strict');
const { test } = require('node:test');
const { planLabels, labelPullRequest } = require('./pr-labels.cjs');

const repository = 'example/integration';

test('one local closing issue owns missing classification, not priority', () => {
  const result = planLabels({
    repository,
    title: 'chore: misleading implementation detail',
    files: ['.github/workflows/ci.yml'],
    current: ['priority:P1', 'help wanted'],
    issues: [{ repository, labels: ['type:bug', 'area:controls', 'area:entities', 'priority:P2'] }],
  });
  assert.deepEqual(result.add, ['type:bug', 'area:controls', 'area:entities']);
});

test('fallback classifies the change, not accompanying tests or documentation', () => {
  assert.deepEqual(planLabels({
    repository,
    title: 'fix(protocol): retain delayed confirmation',
    files: ['custom_components/uplus_reborn/protocol/_protocol.py', 'tests/test_protocol.py', 'README.md'],
  }).add, ['type:bug', 'area:protocol']);
});

test('model-specific protocol documentation receives a protocol area without a linked issue', () => {
  assert.deepEqual(planLabels({
    repository,
    title: 'docs(protocol): clarify captured packets',
    files: ['docs/protocol/hwd120-b1558u.md', 'README.md'],
  }), { add: ['type:docs', 'area:protocol'], notices: [] });
});

test('manual classification wins and repeated runs do not accumulate labels', () => {
  const current = ['type:feature', 'area:localization', 'priority:P0', 'breaking-change'];
  const input = {
    repository, title: 'fix: different implementation', files: ['.github/labels.json'], current,
    issues: [{ repository, labels: ['type:bug', 'area:controls'] }],
  };
  assert.deepEqual(planLabels(input).add, []);
  const first = planLabels({ ...input, current: ['priority:P0'] }).add;
  assert.deepEqual(planLabels({ ...input, current: ['priority:P0', ...first] }).add, []);
  assert.deepEqual(current, ['type:feature', 'area:localization', 'priority:P0', 'breaking-change']);
});

test('foreign or multiple closing issues cannot own classification', () => {
  const input = {
    repository, title: 'ci: label PRs', files: ['.github/workflows/pr-labels.yml'],
  };
  const foreign = { repository: 'other/project', labels: ['type:bug', 'area:protocol'] };
  assert.deepEqual(planLabels({ ...input, issues: [foreign] }).add, ['type:chore', 'area:ci-release']);
  assert.deepEqual(planLabels({
    ...input, issues: [{ ...foreign, repository }, { ...foreign, repository }],
  }).add, ['type:chore', 'area:ci-release']);
});

test('ambiguous groups stay manual rather than choosing an arbitrary label', () => {
  assert.deepEqual(planLabels({
    repository, title: 'fix: change', files: ['tools/pr-labels.cjs'],
    issues: [{ repository, labels: ['type:bug', 'type:feature', 'area:protocol'] }],
  }).add, ['area:protocol']);
  assert.deepEqual(planLabels({
    repository, title: 'fix: cross-cutting change',
    files: ['custom_components/uplus_reborn/select.py', 'custom_components/uplus_reborn/protocol/_protocol.py'],
  }).add, ['type:bug']);
});

test('unknown types and arbitrary input cannot create undeclared or special labels', () => {
  assert.deepEqual(planLabels({
    repository, title: 'urgent: priority:P0; $(touch injected)', files: ['unknown.txt'],
    issues: [{ repository, labels: ['type:not-declared', 'area:not-declared', 'priority:P0', 'status:blocked'] }],
  }).add, []);
});

function apiFixture() {
  const pr = {
    state: 'open', base: { ref: 'master' }, head: { sha: 'initial' },
    title: 'fix: preserve delayed proof', body: '', changed_files: 1,
    labels: [{ name: 'priority:P1' }, { name: 'help wanted' }],
  };
  let writes = 0;
  const github = {
    rest: {
      pulls: {
        get: async () => ({ data: structuredClone(pr) }),
        listFiles: async () => ({ data: [{ filename: 'custom_components/uplus_reborn/protocol/_protocol.py' }] }),
      },
      issues: {
        listLabelsForRepo: async () => ({ data: require('../.github/labels.json') }),
        addLabels: async ({ labels }) => {
          writes++;
          for (const name of labels) {
            if (!pr.labels.some((label) => label.name === name)) pr.labels.push({ name });
          }
        },
      },
    },
    paginate: async (method) => (await method()).data,
    graphql: async () => ({
      repository: { pullRequest: { closingIssuesReferences: { pageInfo: { hasNextPage: false }, nodes: [] } } },
    }),
  };
  const args = {
    github, context: { repo: { owner: 'example', repo: 'integration' }, payload: { pull_request: { number: 7 } } },
    core: { info() {} },
  };
  return { pr, args, writes: () => writes };
}

test('API application preserves manual metadata and reaches an idempotent state', async () => {
  const fixture = apiFixture();
  await labelPullRequest(fixture.args);
  assert.deepEqual(fixture.pr.labels.map(({ name }) => name),
    ['priority:P1', 'help wanted', 'type:bug', 'area:protocol']);
  await labelPullRequest(fixture.args);
  assert.equal(fixture.writes(), 1);
});

test('a push during classification cannot label the new head from old paths', async () => {
  const fixture = apiFixture();
  const query = fixture.args.github.graphql;
  fixture.args.github.graphql = async () => {
    fixture.pr.head.sha = 'new-head';
    return query();
  };
  assert.equal((await labelPullRequest(fixture.args)).status, 'stale');
  assert.equal(fixture.writes(), 0);
});

test('incomplete changed-file data cannot produce a guessed area', async () => {
  const fixture = apiFixture();
  fixture.pr.changed_files = 3001;
  await labelPullRequest(fixture.args);
  assert.deepEqual(fixture.pr.labels.map(({ name }) => name),
    ['priority:P1', 'help wanted', 'type:bug']);
});
