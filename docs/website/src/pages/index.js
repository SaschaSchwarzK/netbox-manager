import React from 'react';
import {Redirect} from '@docusaurus/router';
import useBaseUrl from '@docusaurus/useBaseUrl';

// The docs have no index page, so the site root forwards to the user guide.
export default function Home() {
  return <Redirect to={useBaseUrl('/docs/user-guide')} />;
}
