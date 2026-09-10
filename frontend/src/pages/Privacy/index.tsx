import { Table, TableBody, TableCell, TableHead, TableRow } from "@mui/material"

import LegalPageLayout from "components/common/LegalPageLayout"
import {
  ExternalLink,
  H2,
  H3,
  InternalLink,
  P,
} from "components/common/LegalText"

// Legal copy is owned by the team and pasted verbatim, so it is rendered
// directly rather than through a markdown parser.
const Privacy = () => {
  return (
    <LegalPageLayout title="Privacy Policy" lastUpdated="2026-08-01">
      <P>
        Araya Inc. (&quot;we,&quot; &quot;us,&quot; or &quot;Araya&quot;)
        provides the cloud service &quot;Araya-OptiNiSt Cloud&quot; (the
        &quot;Service&quot;). This Privacy Policy (the &quot;Policy&quot;)
        describes the personal information we collect, how we use and share it,
        including transfers outside Japan, and the choices and rights you have.
        This Policy and the Service interface are provided in English.
      </P>
      <P>
        Operational details of the Service (such as available plans, storage
        limits, supported data formats, and retention periods) may change over
        time. Those details are described on the Service website and in its
        documentation, which are the authoritative and most current source. This
        Policy intentionally refers to them rather than restating values that
        may change.
      </P>

      <H2>1. Operator Information</H2>
      <Table size="small" sx={{ mb: 3 }}>
        <TableHead>
          <TableRow>
            <TableCell sx={{ fontWeight: 600 }}>Item</TableCell>
            <TableCell sx={{ fontWeight: 600 }}>Details</TableCell>
          </TableRow>
        </TableHead>
        <TableBody>
          <TableRow>
            <TableCell>Business operator</TableCell>
            <TableCell>Araya Inc.</TableCell>
          </TableRow>
          <TableRow>
            <TableCell>Representative</TableCell>
            <TableCell>Ryota Kanai, Representative Director (CEO) </TableCell>
          </TableRow>
          <TableRow>
            <TableCell>Registered address</TableCell>
            <TableCell>
              6F Sanpo Sakuma Building, 1-11 Kanda-Sakumacho, Chiyoda-ku, Tokyo
              101-0025, Japan{" "}
            </TableCell>
          </TableRow>
          <TableRow>
            <TableCell>Website</TableCell>
            <TableCell>
              <ExternalLink href="https://www.araya.org/">
                https://www.araya.org/
              </ExternalLink>
            </TableCell>
          </TableRow>
          <TableRow>
            <TableCell>Service site</TableCell>
            <TableCell>
              <ExternalLink href="https://www.araya-optinist.com/">
                https://www.araya-optinist.com/
              </ExternalLink>
            </TableCell>
          </TableRow>
          <TableRow>
            <TableCell>
              Privacy contact / personal information protection manager
            </TableCell>
            <TableCell>
              <ExternalLink href="mailto:optinist-support@araya.org">
                optinist-support@araya.org
              </ExternalLink>
            </TableCell>
          </TableRow>
        </TableBody>
      </Table>

      <H2>2. Information We Collect</H2>
      <P>
        We collect the following categories of information. In this Policy,
        &quot;personal information&quot; carries the meaning given under
        applicable law, and includes &quot;personal data&quot; under the GDPR.
      </P>

      <H3>2.1 Information you provide</H3>
      <ul>
        <li>
          Account information, such as your email address and name.
          Authentication is handled by a third-party authentication provider;
          your password is managed and stored by that provider, and we never
          store or view your plaintext password.
        </li>
        <li>
          Communications, such as the content of your inquiries and support
          requests.
        </li>
      </ul>

      <H3>2.2 Information generated through your use of the Service</H3>
      <ul>
        <li>
          Account and profile data, such as your account identifier, roles, and
          settings or preferences.
        </li>
        <li>
          Workspace and analysis metadata, such as the names, timestamps,
          status, and usage information associated with your workspaces and
          analyses.
        </li>
        <li>
          Subscription and billing status, such as your current plan and its
          status. (Payment card details are handled by our payment processor;
          see Section 4.)
        </li>
        <li>
          Service logs and usage information, such as access logs, identifiers,
          timestamps, diagnostic and error information, and usage metrics that
          we use to operate, secure, and improve the Service.
        </li>
      </ul>

      <H3>2.3 Research data you upload and generate</H3>
      <P>
        You upload research data to the Service to be analyzed, and the Service
        generates results from it. We process this data to provide the Service
        to you.
      </P>
      <P>
        Whether your research data contains personal information depends on how
        you use the Service. If it may contain personal information or
        special-category data relating to identifiable third parties, please see
        the <InternalLink to="/terms">Terms of Service</InternalLink> (including
        the restrictions on the data you may upload) and Section 7 of this
        Policy, and ensure you have the necessary rights and approvals. Wherever
        possible, you should anonymize or de-identify research data before
        uploading it.
      </P>

      <H3>2.4 Device and network information</H3>
      <P>
        When you access the Service, we and our service providers may collect
        device and network information, such as IP address, browser and device
        type, and similar technical information, including through the cookies
        and analytics described in Section 5.
      </P>

      <H3>2.5 Children</H3>
      <P>
        The Service is intended for researchers and is not directed to children.
        We do not knowingly collect personal information from anyone under 18
        years of age (or the higher age of consent where applicable law sets a
        higher one). If you believe a child has provided us with personal
        information, please contact us.
      </P>

      <H2>3. How We Use Information</H2>
      <P>We use the information we collect to:</P>
      <ul>
        <li>
          provide, authenticate, and administer the Service and your account;
        </li>
        <li>
          store, synchronize, analyze, and visualize the research data you
          upload, and deliver results;
        </li>
        <li>
          send service-related and authentication-related notifications (such as
          email verification and password resets);
        </li>
        <li>manage subscriptions and process payments;</li>
        <li>manage storage and compute resources and enforce plan limits;</li>
        <li>
          maintain, secure, and improve the Service, and prevent and detect
          misuse;
        </li>
        <li>respond to your inquiries and provide support; and</li>
        <li>comply with applicable laws and respond to lawful requests.</li>
      </ul>

      <H2>4. How We Share Information and International Transfers</H2>
      <H3>4.1 Service providers (sub-processors)</H3>
      <P>
        We do not sell your personal information. We share information only as
        needed to provide the Service, and with providers who are bound by
        contract to protect it. These providers fall into the following
        categories:
      </P>
      <ul>
        <li>
          Authentication and notification provider: for account sign-in and
          authentication-related email (currently Google LLC / Firebase).
        </li>
        <li>
          Cloud infrastructure provider: for hosting, storage, databases,
          compute, and monitoring (currently Amazon Web Services, Inc.).
        </li>
        <li>
          Payment processing provider: for subscription billing (currently
          Stripe, Inc.). Payment card details are collected and handled directly
          by this provider; we do not store card numbers.
        </li>
        <li>
          Analytics provider: for website and usage analytics (planned: Google
          LLC / Google Analytics, not yet active; see Section 5).
        </li>
      </ul>
      <P>
        We may also disclose personal information when required by law, to
        protect rights, safety, or property, or in connection with a merger or
        other business succession. We may disclose aggregated or de-identified
        information that does not identify you.
      </P>

      <H3>4.2 International (cross-border) transfers of personal data</H3>
      <P>
        Your research data (research content) is stored on our cloud
        infrastructure in the Asia Pacific (Tokyo) region of Amazon Web Services
        (ap-northeast-1), located in Japan. Certain other providers process
        limited personal data outside Japan, as follows. Because your research
        content remains in Japan, and Japan is recognized by the European
        Commission as providing an adequate level of data protection, the
        storage of research content does not rely on Standard Contractual
        Clauses. Certain account, authentication, billing, support, security,
        and technical information may, however, be processed outside Japan by
        our service providers and their authorized subprocessors, as described
        below.
      </P>
      <Table size="small" sx={{ mb: 3 }}>
        <TableHead>
          <TableRow>
            <TableCell sx={{ fontWeight: 600 }}>Provider (role)</TableCell>
            <TableCell sx={{ fontWeight: 600 }}>Processing location</TableCell>
            <TableCell sx={{ fontWeight: 600 }}>Transfer safeguard</TableCell>
          </TableRow>
        </TableHead>
        <TableBody>
          <TableRow>
            <TableCell>
              Google LLC: authentication (analytics planned)
            </TableCell>
            <TableCell>United States and other countries</TableCell>
            <TableCell>
              Google is certified under the EU-U.S. Data Privacy Framework
              (DPF); EU Standard Contractual Clauses apply as a fallback, per
              Google&apos;s data processing terms
            </TableCell>
          </TableRow>
          <TableRow>
            <TableCell>
              Amazon Web Services, Inc.: cloud infrastructure (storage,
              database, compute)
            </TableCell>
            <TableCell>Japan: Asia Pacific (Tokyo) / ap-northeast-1</TableCell>
            <TableCell>
              Data is stored in Japan; for any processing outside Japan, the AWS
              Data Processing Addendum incorporates the EU-U.S. DPF and EU
              Standard Contractual Clauses
            </TableCell>
          </TableRow>
          <TableRow>
            <TableCell>Stripe, Inc.: payment processing</TableCell>
            <TableCell>United States and other countries</TableCell>
            <TableCell>
              Stripe is certified under the EU-U.S. Data Privacy Framework
              (DPF); EU Standard Contractual Clauses apply as a fallback, per
              Stripe&apos;s Data Processing Agreement
            </TableCell>
          </TableRow>
        </TableBody>
      </Table>

      <P>
        <strong>
          For users in Japan (Act on the Protection of Personal Information,
          &quot;APPI&quot;):
        </strong>{" "}
        Your research data (research content) is stored in Japan; certain
        account, authentication, and billing information may be processed
        outside Japan by the providers noted below. Where personal data is
        provided to a third party located in a foreign country (for example, to
        Google and Stripe in the United States for authentication, analytics,
        and payment processing), we obtain your consent to such transfer when
        you create your account and the relevant recipients also maintain
        systems that conform to recognized international frameworks (the EU-U.S.
        Data Privacy Framework and Standard Contractual Clauses). On request, we
        will provide information about the destination country (currently the
        United States), that country&apos;s personal-information-protection
        system, and the measures the recipient takes to protect personal data.
        The Personal Information Protection Commission designates the EU and the
        UK as having a protection regime equivalent to Japan&apos;s. In short,
        your research data remains stored in Japan; the cross-border provision
        of personal data is limited to account, authentication, and billing
        information provided to Google and Stripe (currently in the United
        States).
      </P>
      <P>
        <strong>
          For users in the EEA / UK / Switzerland (GDPR and equivalents):
        </strong>{" "}
        We process personal data in two distinct layers. (1) Research data
        (research content) is stored and processed on Amazon Web Services in the
        Asia Pacific (Tokyo) region, in Japan. Transfers of this data from the
        EEA/UK/Switzerland to Japan rely on the European Commission&apos;s
        adequacy decision for Japan (and the equivalent UK and Swiss
        recognitions), so they do not require Standard Contractual Clauses; your
        research data itself is not transferred to the United States or other
        third countries. (2) A limited set of personal data (such as your name,
        email address, authentication tokens, and payment-related information,
        but not your research data) is processed by Google (Firebase) and
        Stripe, which may process it in the United States or other countries.
        These transfers rely on the recipients&apos; EU-U.S. Data Privacy
        Framework certifications and/or the European Commission&apos;s 2021
        Standard Contractual Clauses (with the UK Addendum and Swiss
        amendments), as set out in each provider&apos;s data processing terms.
        Where required, we will carry out a transfer impact assessment in
        respect of these onward transfers. Because the Service is operated from
        Japan and is not directed at, and does not monitor the behaviour of,
        individuals in the EEA/UK, we have not appointed a representative under
        GDPR Article 27; we will reassess this if our activities change.
      </P>

      <H3>4.3 Processing on behalf of organizations</H3>
      <P>
        Where you use the Service under an arrangement with an organization
        (such as your university, institute, or employer) that determines the
        purposes of processing, that organization may be the data controller and
        we may act as a processor on its behalf. In such cases, a separate Data
        Processing Agreement may govern and will prevail for that processing.
        See the Terms of Service. For account administration, billing, security,
        support, legal compliance, and our own service operations, Araya
        generally acts as a controller. Where we process research data solely on
        documented instructions from an organizational customer, Araya acts as a
        processor, subject to the applicable DPA.
      </P>

      <H2>5. Cookies, Analytics, and Similar Technologies</H2>
      <H3>5.1 Strictly necessary storage</H3>
      <P>
        To keep you signed in, the Service stores authentication tokens in your
        browser&apos;s local storage or similar. These are strictly necessary to
        use the Service.
      </P>

      <H3>5.2 Analytics (Google Analytics)</H3>
      <P>
        We plan to use Google Analytics, provided by Google LLC, to understand
        how the Service is used and to improve it (this analytics integration is
        not yet active on the Service). Google Analytics uses cookies and
        similar technologies to collect information such as pages viewed,
        interactions, approximate location derived from IP address, and device
        and browser information. This information is transmitted to and stored
        by Google. For details, see Google&apos;s{" "}
        <ExternalLink href="https://policies.google.com/privacy">
          Privacy Policy
        </ExternalLink>{" "}
        and{" "}
        <ExternalLink href="https://policies.google.com/technologies/partner-sites">
          &quot;How Google uses information from sites or apps that use our
          services&quot;
        </ExternalLink>
        . You can opt out using the{" "}
        <ExternalLink href="https://tools.google.com/dlpage/gaoptout">
          Google Analytics Opt-out Browser Add-on
        </ExternalLink>
        , or by declining analytics cookies through the consent banner described
        below.
      </P>
      <P>
        Consent: Before activating analytics, we will use a cookie-consent
        banner that requests your prior opt-in consent for analytics cookies if
        you are in the EEA or the UK, and that lets you decline or withdraw
        consent at any time; elsewhere, analytics may run on an opt-out basis.
        Google Analytics will be configured with IP-address anonymization
        enabled, a data-retention period of 14 months, and Google Signals /
        advertising features disabled.
      </P>

      <H2>6. Data Sharing and Publishing Features</H2>
      <P>
        The Service includes optional features that let you share or publish
        data; using them is your choice.
      </P>
      <ul>
        <li>
          Workspace sharing: You can share a workspace with specific other
          users, who then gain access to the data within it.
        </li>
        <li>
          Public publishing: You can publish results so they are viewable by
          anyone, including people without an account. Publishing happens only
          through your explicit action, and you can make content private again.
          Note that we cannot retrieve or delete copies that third parties may
          have made while the content was public.
        </li>
      </ul>
      <P>
        If you use these features, you are responsible for ensuring that the
        shared or published data does not contain personal information of third
        parties without an appropriate basis.
      </P>

      <H2>7. Data Retention and Deletion</H2>
      <ul>
        <li>
          Deletion you initiate: You can delete your account and your data. When
          you do, we remove the data from the Service&apos;s active systems.
        </li>
        <li>
          Automatic deletion: If your stored data exceeds the limit applicable
          to your plan (for example, after a paid subscription ends and a grace
          period passes), data may be automatically deleted to bring usage
          within the limit. You can choose, in your account settings, which
          categories of data to preserve. The grace period, applicable limits,
          and deletion behavior are described in the Service documentation.
        </li>
        <li>
          Residual copies and backups: After deletion from active systems,
          residual copies may remain for a limited period in encrypted backups
          and in versioned object storage, after which they are deleted in the
          ordinary course. Automated database backups are retained for up to 35
          days. Object storage retains prior (non-current) versions of files. We
          do not use such copies to reinstate deleted data except as necessary
          to recover from a system failure, and we do not otherwise restore
          deleted data.
        </li>
        <li>
          Retention: We retain account and usage information for as long as
          needed to provide the Service. Billing and transaction records are
          retained for 7 years to comply with Japanese tax and corporate
          record-keeping requirements.
        </li>
      </ul>

      <H2>8. Security</H2>
      <P>
        We maintain technical and organizational measures designed to protect
        information, including encryption in transit and at rest, access
        controls, secure credential management, and delegating password
        management to our authentication provider so that passwords are not
        stored on our servers. No method of transmission or storage is
        completely secure, and we cannot guarantee absolute security.
      </P>
      <P>
        We describe our principal safety-management measures in this Policy and
        will provide further detail on request to the contact in Section 10, to
        the extent required by applicable law (including the APPI).
      </P>
      <P>
        In the event of a data breach affecting your personal information, we
        will notify the relevant authority (such as Japan&apos;s Personal
        Information Protection Commission) and affected individuals as, and to
        the extent, required by applicable law.
      </P>

      <H2>9. Your Rights</H2>
      <P>
        Subject to applicable law, you may request access to, correction of,
        deletion of, or restriction of the processing of your personal
        information, and may object to certain processing. To make a request,
        contact us at the address in Section 10; we will respond in accordance
        with applicable law after verifying your identity.
      </P>
      <P>
        Where the GDPR (or an equivalent law) applies, you also have the rights
        to access, rectification, erasure, restriction, data portability, and
        objection, the right to withdraw consent at any time, and the right to
        lodge a complaint with your data-protection supervisory authority. We
        process personal data on the following legal bases: performance of our
        contract with you (to provide the Service); your consent (for example,
        analytics cookies); and our legitimate interests (to secure, maintain,
        and improve the Service).
      </P>
      <P>
        Where your research data includes special-category data (Article 9 GDPR)
        or personal information requiring special care (APPI), you (or the
        organization you represent) act as the data controller and are
        responsible for ensuring an appropriate legal basis or exception for
        such processing (for example, explicit consent or a scientific-research
        basis). We act as a data processor and process such data on your
        documented instructions to provide the Service. Certain such data may be
        uploaded only under a separate written agreement (such as a DPA); see
        the Terms of Service, Section 7.
      </P>

      <H2>10. Contact</H2>
      <P>For questions about this Policy or your personal information:</P>
      <P>
        Araya Inc., Araya-OptiNiSt Cloud Support
        <br />
        Email:{" "}
        <ExternalLink href="mailto:optinist-support@araya.org">
          optinist-support@araya.org
        </ExternalLink>
      </P>

      <H2>11. Changes to This Policy</H2>
      <P>
        We may update this Policy from time to time. We will indicate the
        &quot;Last updated&quot; date above and, for material changes, provide
        notice by posting on the Service or by other appropriate means.
      </P>
    </LegalPageLayout>
  )
}

export default Privacy
