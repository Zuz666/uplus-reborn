// Metadata only: never load or execute files from a pull request head.
const declared = new Set(require('../.github/labels.json').map(({ name }) => name));
const types = {
  fix: 'bug', feat: 'feature', docs: 'docs', test: 'test', refactor: 'refactor',
  perf: 'refactor', build: 'chore', ci: 'chore', chore: 'chore', style: 'chore', revert: 'chore',
};

function pathAreas(files) {
  const nonTests = files.filter((path) => !/^(tests\/|tools\/(?:test_|.*\.test\.))/.test(path));
  if (files.length && !nonTests.length) return ['area:testing'];
  const implementation = nonTests.filter((path) => !path.endsWith('.md'));
  const areas = new Set();
  for (const path of implementation.length ? implementation : nonTests) {
    if (path.startsWith('custom_components/uplus_reborn/protocol/') || path === 'docs/protocol.md') {
      areas.add('area:protocol');
    } else if (path.startsWith('custom_components/uplus_reborn/translations/') || path.startsWith('docs/terminology/')) {
      areas.add('area:localization');
    } else if (/^custom_components\/uplus_reborn\/(discovery|config_flow)\.py$/.test(path)) {
      areas.add('area:discovery');
    } else if (path.startsWith('custom_components/uplus_reborn/')) {
      areas.add('area:entities');
      if (/\/(button|select|switch|number)\.py$/.test(path)) areas.add('area:controls');
    } else if (path === 'SECURITY.md') {
      areas.add('area:security');
    } else if (/^(\.github\/|tools\/)/.test(path)
      || /^(package(-lock)?\.json|pyproject\.toml|uv\.lock|hacs\.json|\.coderabbit\.yaml)$/.test(path)
      || /^docs\/(development-verification|release-workflow)\.md$/.test(path)) {
      areas.add('area:ci-release');
    }
  }
  return [...areas];
}

function planLabels({ repository, title = '', files = [], current = [], issues = [] }) {
  const add = [];
  const notices = [];
  const issue = issues.length === 1 && issues[0].repository.toLowerCase() === repository.toLowerCase()
    ? issues[0] : null;
  const titleType = types[/^([a-z]+)(?:\([^\r\n)]+\))?!?:[ \t]+\S/.exec(title)?.[1]];
  const fallback = { 'type:': titleType ? [`type:${titleType}`] : [], 'area:': pathAreas(files) };
  for (const [prefix, maximum] of [['type:', 1], ['area:', 2]]) {
    if (current.some((label) => label.startsWith(prefix))) continue;
    const issueGroup = (issue?.labels ?? []).filter((label) => label.startsWith(prefix));
    const candidates = [...new Set((issueGroup.length ? issueGroup : fallback[prefix])
      .filter((label) => declared.has(label)))];
    if (candidates.length > maximum) {
      notices.push(`Ambiguous ${prefix} group; classify manually.`);
    } else {
      add.push(...candidates);
      if (!candidates.length) notices.push(`No unambiguous ${prefix} label; classify manually.`);
    }
  }
  return { add, notices };
}

async function labelPullRequest({ github, context, core }) {
  const number = Number(context.payload.pull_request?.number ?? context.payload.inputs?.pull_request);
  if (!Number.isSafeInteger(number) || number <= 0) throw new Error('A positive PR number is required.');
  const params = { ...context.repo, pull_number: number };
  const { data: initial } = await github.rest.pulls.get(params);
  if (initial.state !== 'open' || initial.base.ref !== 'master') return { status: 'skipped' };
  const files = await github.paginate(github.rest.pulls.listFiles, { ...params, per_page: 100 });
  const linked = await github.graphql(`
    query($owner: String!, $repo: String!, $number: Int!) {
      repository(owner: $owner, name: $repo) {
        pullRequest(number: $number) {
          closingIssuesReferences(first: 2) {
            pageInfo { hasNextPage }
            nodes {
              repository { nameWithOwner }
              labels(first: 100) { pageInfo { hasNextPage } nodes { name } }
            }
          }
        }
      }
    }
  `, { ...context.repo, number });
  const references = linked.repository.pullRequest.closingIssuesReferences;
  const issues = !references.pageInfo.hasNextPage
    && references.nodes.every((issue) => !issue.labels.pageInfo.hasNextPage)
    ? references.nodes.map((issue) => ({
      repository: issue.repository.nameWithOwner, labels: issue.labels.nodes.map(({ name }) => name),
    })) : [];
  const available = new Set((await github.paginate(
    github.rest.issues.listLabelsForRepo, { ...context.repo, per_page: 100 },
  )).map(({ name }) => name));
  // A human can edit metadata while files/linked issues are being fetched.
  const { data: latest } = await github.rest.pulls.get(params);
  if (latest.state !== 'open' || latest.base.ref !== 'master'
    || latest.head.sha !== initial.head.sha || latest.title !== initial.title
    || latest.body !== initial.body) {
    core.info('PR changed while classifying; a fresh event/dispatch must handle it.');
    return { status: 'stale' };
  }
  const completeFiles = files.length === latest.changed_files;
  const plan = planLabels({
    repository: `${context.repo.owner}/${context.repo.repo}`,
    title: latest.title,
    files: completeFiles ? files.flatMap(({ filename, previous_filename }) => previous_filename
      ? [filename, previous_filename] : [filename]) : [],
    current: latest.labels.map(({ name }) => name),
    issues,
  });
  if (!completeFiles) plan.notices.push('Changed-file data is incomplete; path fallback is disabled.');
  const add = plan.add.filter((label) => available.has(label));
  if (add.length !== plan.add.length) {
    plan.notices.push('Some declared labels are absent; run Synchronize labels before repair.');
  }
  if (add.length) {
    // Add, never set/replace: manual priority and special labels remain untouched.
    await github.rest.issues.addLabels({ ...context.repo, issue_number: number, labels: add });
  }
  const result = { status: 'complete', pull_request: number, added: add, notices: plan.notices };
  core.info(JSON.stringify(result));
  return result;
}

module.exports = { planLabels, labelPullRequest };
