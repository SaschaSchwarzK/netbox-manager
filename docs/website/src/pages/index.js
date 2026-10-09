import React from 'react';
import Layout from '@theme/Layout';
import Link from '@docusaurus/Link';
import styles from './index.module.css';

const repositoryUrl = 'https://github.com/SaschaSchwarzK/netbox-manager';

const capabilities = [
  { title: 'Configuration as code', description: 'Manage device types, custom fields, reference data, and tenant permissions as reviewable YAML in GitHub.' },
  { title: 'Safe fleet publishing', description: 'Preview differences, open pull requests, enforce approval gates, publish to selected NetBox instances, and detect drift.' },
  { title: 'Search and fleet insight', description: 'Search infrastructure across instances and inspect health, versions, plugins, response times, and device-type coverage.' },
  { title: 'Migration and export', description: 'Plan tenant-scoped migrations with mapping review and export inventory to CSV or Excel using standard and custom fields.' },
  { title: 'Access automation', description: 'Apply Git-backed tenant permission templates while scoping user roles to the instances and repositories they need.' },
  { title: 'Auditability', description: 'Keep an audit trail, attribute pull requests to their requesters, and optionally forward audit events to syslog.' },
];

export default function Home() {
  return (
    <Layout title="Git-backed management for NetBox" description="NetBox Manager provides a web UI and Git-backed control plane for managing multiple NetBox instances.">
      <main>
        <header className={styles.hero}>
          <div className={styles.heroContent}>
            <p className={styles.eyebrow}>One control plane for your NetBox fleet</p>
            <h1>Manage shared NetBox configuration with Git-backed review.</h1>
            <p className={styles.intro}>NetBox Manager helps teams create, review, publish, search, migrate, and export data across multiple NetBox instances—without giving up GitHub as the source of truth.</p>
            <div className={styles.actions}>
              <Link className="button button--primary button--lg" to="/docs/user-guide">Read the user guide</Link>
              <Link className="button button--secondary button--lg" href={repositoryUrl}>View on GitHub</Link>
            </div>
          </div>
        </header>

        <section className={styles.section}>
          <div className="container">
            <div className={styles.sectionHeading}>
              <p className={styles.eyebrow}>Capabilities</p>
              <h2>From reviewed configuration to daily operations</h2>
              <p>Use one interface to coordinate consistent changes and operational work across your NetBox environments.</p>
            </div>
            <div className={styles.grid}>
              {capabilities.map((capability) => (
                <article className={styles.card} key={capability.title}>
                  <h3>{capability.title}</h3>
                  <p>{capability.description}</p>
                </article>
              ))}
            </div>
          </div>
        </section>

        <section className={styles.projectSection}>
          <div className={`container ${styles.projectGrid}`}>
            <div>
              <p className={styles.eyebrow}>Open source</p>
              <h2>Explore and contribute</h2>
              <p>Source code, issue tracking, and releases are available in the <Link href={repositoryUrl}>NetBox Manager GitHub repository</Link>. The project is distributed under the <Link href={`${repositoryUrl}/blob/main/LICENSE`}>MIT License</Link>.</p>
            </div>
            <div className={styles.projectCards}>
              <aside className={styles.authorCard}>
                <span>Created and maintained by</span>
                <strong>Sascha Schwarz</strong>
                <a href="mailto:sascha@providerhoelle.de">sascha@providerhoelle.de</a>
              </aside>
              <aside className={styles.supportCard}>
                <strong>Support the project</strong>
                <span>
                  If NetBox Manager is useful to you, you can make a voluntary donation via{' '}
                  <Link href="https://paypal.me/3dnerd">PayPal</Link>.
                </span>
                <small>Donations do not purchase support or services and do not change the MIT License.</small>
              </aside>
            </div>
          </div>
        </section>
      </main>
    </Layout>
  );
}
