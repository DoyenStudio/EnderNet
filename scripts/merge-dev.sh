# Install this script as the local Git alias documented in README.md.
# Git runs ! aliases in its bundled POSIX shell, including on Windows.
merge_dev() {
    branch=$(git symbolic-ref --quiet --short HEAD) || return 1
    if [ "$branch" != master ]; then
        echo 'Run git merge-dev on master.' >&2
        return 1
    fi
    if [ -n "$(git status --porcelain)" ] ||
       git rev-parse --verify --quiet MERGE_HEAD >/dev/null; then
        echo 'Finish any pending merge and commit or stash all changes first.' >&2
        return 1
    fi
    git rev-parse --verify 'dev^{commit}' >/dev/null || return 1
    git merge --no-ff --no-commit dev
    merge_status=$?
    if ! git rev-parse --verify --quiet MERGE_HEAD >/dev/null; then
        return "$merge_status"
    fi

    # Include HEAD paths as well as index paths to handle additions, deletions,
    # renames, and conflicts. Restore only plans; leave other merge results alone.
    if [ -n "$(git ls-files -- plans)$(git ls-tree HEAD -- plans)" ]; then
        git restore --source=HEAD --staged --worktree -- plans || {
            echo 'Could not restore plans. Run git merge --abort before retrying.' >&2
            return 1
        }
    fi
    if [ -n "$(git ls-files --unmerged)" ]; then
        echo 'plans preserved. Resolve remaining conflicts, then git commit.' >&2
        return 1
    fi
    git commit -m 'Merge dev into master (preserve master plans)'
}
merge_dev
