//! Small deterministic DAG; each node stores its dependencies.
use anyhow::{Result, bail};
use std::collections::{BTreeMap, BTreeSet};

#[derive(Default)]
pub(crate) struct Graph {
    dependencies: BTreeMap<String, BTreeSet<String>>,
}

impl Graph {
    pub(crate) fn node(&mut self, node: impl Into<String>) {
        self.dependencies.entry(node.into()).or_default();
    }

    pub(crate) fn edge(&mut self, node: &str, dependency: &str) -> Result<()> {
        self.node(node);
        self.node(dependency);
        if let Some(path) = self.path(dependency, node) {
            bail!("dependency cycle: {} -> {}", node, path.join(" -> "));
        }
        self.dependencies
            .entry(node.into())
            .or_default()
            .insert(dependency.into());
        Ok(())
    }

    fn path(&self, start: &str, target: &str) -> Option<Vec<String>> {
        let mut stack = vec![(start.to_owned(), vec![start.to_owned()])];
        let mut seen = BTreeSet::new();
        while let Some((node, path)) = stack.pop() {
            if node == target {
                return Some(path);
            }
            if !seen.insert(node.clone()) {
                continue;
            }
            if let Some(deps) = self.dependencies.get(&node) {
                for dep in deps.iter().rev() {
                    let mut next = path.clone();
                    next.push(dep.clone());
                    stack.push((dep.clone(), next));
                }
            }
        }
        None
    }

    pub(crate) fn order(&self) -> Result<Vec<String>> {
        let mut remaining = self.dependencies.clone();
        let mut ordered = Vec::new();
        while !remaining.is_empty() {
            let ready: Vec<_> = remaining
                .iter()
                .filter(|(_, deps)| deps.is_empty())
                .map(|(name, _)| name.clone())
                .collect();
            if ready.is_empty() {
                bail!("dependency cycle");
            }
            for name in &ready {
                remaining.remove(name);
            }
            for deps in remaining.values_mut() {
                for name in &ready {
                    deps.remove(name);
                }
            }
            ordered.extend(ready);
        }
        Ok(ordered)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn deterministic_order_and_cycle() {
        let mut g = Graph::default();
        g.edge("c", "b").unwrap();
        g.edge("b", "a").unwrap();
        assert_eq!(g.order().unwrap(), ["a", "b", "c"]);
        assert!(
            g.edge("a", "c")
                .unwrap_err()
                .to_string()
                .contains("dependency cycle")
        );
        assert!(g.edge("a", "a").is_err());
    }
}
