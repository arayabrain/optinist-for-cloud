import LegalPageLayout from "components/common/LegalPageLayout"
import { ExternalLink, H2, InternalLink, P } from "components/common/LegalText"

// Legal copy is owned by the team and pasted verbatim, so it is rendered
// directly rather than through a markdown parser.
const Terms = () => {
  return (
    <LegalPageLayout title="Terms of Service" lastUpdated="2026-08-01">
      <P>
        These Terms of Service (the &quot;Terms&quot;) govern your use of
        &quot;Araya-OptiNiSt Cloud&quot; (the &quot;Service&quot;), provided by
        Araya Inc. (&quot;we,&quot; &quot;us,&quot; or &quot;Araya&quot;). By
        using the Service, you agree to these Terms. The Terms and the Service
        interface are provided in English.
      </P>
      <P>
        Operational details of the Service (such as available plans, fees,
        storage limits, and retention periods) are described on the Service
        website and in its documentation, which are the authoritative and most
        current source. These Terms refer to them rather than restating values
        that may change.
      </P>

      <H2>1. Definitions</H2>
      <ul>
        <li>
          &quot;Service&quot; means &quot;Araya-OptiNiSt Cloud,&quot; our cloud
          service for analyzing, visualizing, and managing research data such as
          calcium imaging data.
        </li>
        <li>
          &quot;User&quot; (or &quot;you&quot;) means any individual or
          organization that agrees to these Terms and uses the Service.
        </li>
        <li>
          &quot;User Data&quot; means the data you upload to or generate on the
          Service, including analysis results.
        </li>
        <li>
          &quot;Workspace&quot; means the unit in which you manage your data and
          analyses.
        </li>
        <li>
          &quot;Personal data&quot; means information relating to an identified
          or identifiable individual, as defined under applicable
          data-protection law (including &quot;personal information&quot; and
          &quot;personal information requiring special care&quot; under
          Japan&apos;s APPI).
        </li>
      </ul>

      <H2>2. Acceptance of the Terms</H2>
      <P>
        By using the Service, you agree to these Terms. If you do not agree, you
        may not use the Service.
      </P>

      <H2>3. Account Registration</H2>
      <P>
        The Service is intended for users who are at least 18 years old. You
        must be at least 18 years of age (or older, where the age of majority in
        your jurisdiction is higher) and able to form a legally binding contract
        to use the Service. The Service is not available to, and must not be
        used by, individuals under 18.
      </P>
      <ul>
        <li>
          Using the Service requires an account. Authentication is provided
          through a third-party authentication provider.
        </li>
        <li>
          You agree to provide accurate and current information when
          registering.
        </li>
        <li>You may be required to verify your email address.</li>
        <li>
          You are responsible for keeping your account credentials secure and
          must not let any third party use them.
        </li>
        <li>
          You are responsible for activity under your account, including any
          damage arising from inadequate management of your account or its use
          by a third party.
        </li>
      </ul>

      <H2>4. Fees, Subscriptions, and Payment</H2>
      <P>
        The Service offers one or more plans. The current plans, features, and
        fees are shown on the{" "}
        <ExternalLink href="https://www.araya-optinist.com/subscription">
          subscription page
        </ExternalLink>{" "}
        and at checkout.
      </P>
      <ul>
        <li>
          Billing and auto-renewal. Paid plans are offered on a recurring
          subscription basis and renew automatically at the end of each billing
          period until cancelled. Payment is handled by a third-party payment
          processor; we do not store card numbers.
        </li>
        <li>
          Free trial. A free trial may be offered to first-time subscribers. A
          valid payment method is required to start a trial. Unless you cancel
          before the trial ends, the subscription automatically converts to a
          paid subscription and your payment method is charged.
        </li>
        <li>
          Cancellation. You may cancel at any time from your account.
          Cancellation takes effect at the end of the current billing period;
          you retain access to paid features until then and are not charged for
          subsequent periods. You may reverse a scheduled cancellation before
          the period ends.
        </li>
        <li>
          Refunds. Except where required by applicable law, payments are
          non-refundable. Cancelling stops future renewals but does not refund
          amounts already paid for the current period.
        </li>
        <li>
          Price changes. We may change plan fees. Any change applies from your
          next billing period, and where required by law we will provide advance
          notice. Current fees are always shown on the subscription page.
        </li>
        <li>
          Failed payments. If a renewal payment fails, payment may be retried;
          if it is not completed, your paid subscription may end and your
          account may revert to the free plan. Data handling after a paid plan
          ends is described in Section 9 and the Service documentation.
        </li>
      </ul>

      <H2>5. Plans, Storage, and Compute Resources</H2>
      <P>
        Each plan may set limits on storage and compute resources. The current
        limits are described on the subscription page and in the Service
        documentation. You agree to use the Service within your applicable
        limits and not to circumvent them.
      </P>
      <P>
        When your usage reaches an applicable limit, uploading data and other
        functions may be restricted.
      </P>
      <P>
        Depending on your plan, compute resources may be shared with other users
        and subject to defined constraints, or dedicated to your account.
      </P>

      <H2>6. Handling of User Data</H2>
      <ul>
        <li>
          Ownership: Rights to User Data belong to you or the rightful owner. We
          handle User Data only as needed to provide the Service (including
          storage, synchronization, analysis, visualization, backup, and fault
          handling).
        </li>
        <li>
          Storage: User Data is stored and processed using our cloud
          infrastructure provider, in the Asia Pacific (Tokyo) region (Japan).
          Information about international processing is described in the Privacy
          Policy. Japan is recognized by the European Commission as ensuring an
          adequate level of data protection; see the Privacy Policy (Section
          4.2) for details on international transfers.
        </li>
        <li>
          Sharing and publishing: At your own responsibility, you may share a
          workspace with other users or publish results. Published results may
          be viewable by anyone, including people without an account. You
          control these settings.
        </li>
        <li>
          Third-party rights: You represent and warrant that you hold all rights
          necessary to upload, publish, or share User Data, including any
          third-party intellectual property rights, the lawful handling of any
          personal data, and any required research-ethics approvals.
        </li>
        <li>
          Roles (controller / processor): As between you and us, to the extent
          User Data contains personal data, you (or the organization you
          represent) act as the data controller and we act as a data processor
          that processes such personal data on your documented instructions to
          provide the Service, except where applicable law requires otherwise.
        </li>
        <li>
          Data Processing Agreement: Where required for the processing of
          personal data (for example, for research-institution or other
          organizational customers), the parties will enter into a separate Data
          Processing Agreement (&quot;DPA&quot;). The DPA prevails over these
          Terms with respect to the processing of personal data.
        </li>
        <li>
          Personal information: The handling of personal information is
          otherwise governed by our{" "}
          <InternalLink to="/privacy">Privacy Policy</InternalLink>.
        </li>
      </ul>

      <H2>7. Restrictions on the Data You Upload</H2>
      <P>
        You are responsible for ensuring that User Data complies with applicable
        law and these Terms. Wherever possible, you should anonymize or
        de-identify data before uploading it.
      </P>
      <P>
        Restricted data. Unless we have expressly agreed otherwise in writing
        (for example, under a DPA), you must not upload to the Service:
      </P>
      <ul>
        <li>
          special-category or sensitive personal data (such as health, genetic,
          or biometric data) relating to identifiable individuals; or
        </li>
        <li>
          &quot;personal information requiring special care&quot;
          (要配慮個人情報) under Japan&apos;s APPI relating to identifiable
          individuals.
        </li>
      </ul>
      <P>
        Such restricted data may be processed only under a separate written
        agreement (such as a DPA) with appropriate safeguards. Anonymized data
        that cannot reasonably be linked to an identified or identifiable
        individual is not subject to this restriction. Pseudonymized or
        de-identified data remains subject to applicable data-protection law
        where it can be attributed to an individual using additional information
        or other reasonably available means. If your intended use requires
        processing of the restricted data above, contact us before uploading so
        that appropriate agreements and safeguards can be put in place. This
        restriction is based on the sensitivity of the data rather than the
        geographic origin of the data subjects: research data is stored solely
        in the AWS Tokyo region (Japan), for which the European Commission
        recognizes an adequate level of data protection.
      </P>
      <P>
        Personal data relating to individuals in the EEA, the United Kingdom, or
        Switzerland is not prohibited solely because of the individuals&apos;
        location. However, Restricted Data may not be uploaded unless Araya has
        given prior written approval, an applicable DPA has been entered into,
        and any required privacy and security review has been completed. If your
        User Data contains personal data of individuals in the EEA, the United
        Kingdom, or Switzerland, you remain responsible, as the data controller,
        for ensuring an appropriate legal basis under the GDPR, for providing
        any required information to data subjects, and for obtaining any
        necessary research-ethics approvals.
      </P>
      <P>
        We may remove, restrict access to, or refuse to process User Data that
        we reasonably believe violates this Section or applicable law.
      </P>

      <H2>8. Prohibited Conduct</H2>
      <P>In using the Service, you must not:</P>
      <ul>
        <li>violate any law or public order and morals;</li>
        <li>
          infringe the intellectual property, privacy, reputation, or other
          rights of us or any third party;
        </li>
        <li>
          upload, publish, or share data without the lawful rights or consent to
          do so;
        </li>
        <li>
          place excessive load on the Service, gain unauthorized access, reverse
          engineer (except as permitted by law), or otherwise interfere with the
          Service;
        </li>
        <li>
          distribute malware or engage in other harmful conduct through the
          Service;
        </li>
        <li>improperly circumvent allocated compute or storage limits; or</li>
        <li>
          engage in any other conduct that we reasonably deem inappropriate.
        </li>
      </ul>

      <H2>9. Data Retention and Deletion</H2>
      <ul>
        <li>
          Deletion you initiate: You may delete your account, workspaces, and
          data. Deletion removes the relevant data from the Service&apos;s
          active systems.
        </li>
        <li>
          Automatic deletion: If your stored data exceeds the limit applicable
          to your plan (for example, after a paid subscription ends and a grace
          period passes), data may be automatically deleted to bring usage
          within the limit. You may be able to choose which categories of data
          to preserve. The grace period and deletion behavior are described in
          the Service documentation.
        </li>
        <li>
          Residual copies: After deletion from active systems, residual copies
          may remain for a limited period in backups and versioned storage
          before being deleted in the ordinary course, as described in the
          Privacy Policy.
        </li>
        <li>
          Your responsibility: You are responsible for backing up any data you
          need before any applicable deletion.
        </li>
        <li>
          No restoration: We are under no obligation to restore deleted data.
        </li>
      </ul>

      <H2>10. Intellectual Property and Software License</H2>
      <P>
        The OptiNiSt software underlying the Service is licensed under
        open-source terms (GNU General Public License v3), and its source code
        is available at the project&apos;s public repository (
        <ExternalLink href="https://github.com/arayabrain/araya-optinist">
          https://github.com/arayabrain/araya-optinist
        </ExternalLink>
        ).
      </P>
      <P>
        These Terms govern your use of the hosted Service and do not limit your
        rights in the OptiNiSt software itself under the GPL v3. Rights to
        trademarks, logos, and content related to the Service and its
        documentation belong to us or the rightful owners.
      </P>

      <H2>11. Disclaimers and No Warranty</H2>
      <P>
        The Service is provided &quot;AS IS&quot; as a tool for research
        purposes. We make no warranties, express or implied, regarding the
        accuracy, completeness, fitness for a particular purpose, continuity, or
        freedom from errors or interruptions of the Service or its results.
      </P>
      <P>
        You are responsible for verifying and using the results produced by the
        Service.
      </P>
      <P>
        We may temporarily suspend or modify all or part of the Service due to
        maintenance, faults, specification changes, or matters relating to
        third-party providers.
      </P>

      <H2>12. Limitation of Liability</H2>
      <P>
        To the maximum extent permitted by applicable law, our total aggregate
        liability arising out of or relating to the Service or these Terms will
        not exceed the greater of (a) the total fees you paid for the Service in
        the 12 months before the event giving rise to the claim, or (b) JPY
        10,000.
      </P>
      <P>
        We will not be liable for any indirect, incidental, special,
        consequential, or punitive damages, or for any loss of data, profits, or
        goodwill.
      </P>
      <P>
        Nothing in these Terms excludes or limits liability that cannot be
        excluded or limited under applicable law, including liability for
        willful misconduct or gross negligence, or a consumer&apos;s rights
        under the Consumer Contract Act of Japan.
      </P>

      <H2>13. Indemnification</H2>
      <P>
        To the extent permitted by applicable law, you will indemnify and hold
        us harmless from and against claims, losses, liabilities, and reasonable
        expenses arising out of or relating to (a) your User Data, (b) your
        breach of these Terms (including the data-upload restrictions in Section
        7), or (c) your violation of any law or any third-party right. This
        indemnity does not apply to the extent a loss results from our own
        willful misconduct or gross negligence and, where you are an individual
        consumer, applies only to the extent permitted by the Consumer Contract
        Act of Japan.
      </P>

      <H2>14. Suspension and Termination of Your Account</H2>
      <P>
        We may suspend or terminate your access to the Service, in whole or in
        part, if you breach these Terms (including the data-upload restrictions
        in Section 7 or the prohibited conduct in Section 8), if you fail to pay
        applicable fees, if required by law, or where reasonably necessary to
        protect the Service or other users. Where practicable and not prohibited
        by law, we will give you notice and, for breaches capable of cure, an
        opportunity to cure.
      </P>
      <P>You may stop using the Service and delete your account at any time.</P>
      <P>
        On termination, your right to use the Service ends. We will handle your
        data as described in Section 9 and the Privacy Policy; you are
        responsible for exporting any data you wish to keep before termination.
      </P>
      <P>
        Suspension or termination does not entitle you to a refund except where
        required by applicable law.
      </P>

      <H2>15. Changes, Suspension, and Termination of the Service</H2>
      <P>
        We may change, suspend, or terminate the Service, with prior notice to
        users except in emergencies. We are not liable for any resulting damage
        beyond the scope set out in Section 12.
      </P>

      <H2>16. Changes to These Terms</H2>
      <P>
        We may revise these Terms from time to time. For material changes, we
        will provide notice by posting on the Service or by other appropriate
        means. If you continue to use the Service after a change takes effect,
        you are deemed to have agreed to the revised Terms.
      </P>

      <H2>17. Governing Law and Jurisdiction</H2>
      <P>These Terms are governed by the laws of Japan.</P>
      <P>
        Any dispute relating to the Service shall be subject to the exclusive
        jurisdiction of the Tokyo District Court as the court of first instance.
      </P>
      <P>
        Nothing in this Section deprives a consumer of the protection afforded
        by the mandatory provisions of the law of the consumer&apos;s country of
        habitual residence.
      </P>

      <H2>18. General</H2>
      <ul>
        <li>
          Severability: If any provision of these Terms is held invalid or
          unenforceable, the remaining provisions remain in effect.
        </li>
        <li>
          No waiver: Our failure to enforce any provision is not a waiver of it.
        </li>
        <li>
          Assignment: You may not assign these Terms without our consent; we may
          assign them in connection with a merger, acquisition, or transfer of
          the Service.
        </li>
        <li>
          Entire agreement: These Terms, together with the Privacy Policy and
          any applicable DPA, constitute the entire agreement between you and us
          regarding the Service.
        </li>
        <li>
          Force majeure: We are not liable for any delay or failure to perform
          caused by events beyond our reasonable control.
        </li>
        <li>
          Survival: Provisions that by their nature should survive termination,
          including those on ownership and handling of User Data, intellectual
          property, disclaimers, limitation of liability, indemnification, and
          governing law, survive termination of these Terms or your account.
        </li>
        <li>
          Notices: We may provide notices to you by email or by posting on the
          Service.
        </li>
      </ul>

      <H2>19. Contact</H2>
      <P>For questions about these Terms:</P>
      <P>
        Araya Inc., Araya-OptiNiSt Cloud Support
        <br />
        Email:{" "}
        <ExternalLink href="mailto:optinist-support@araya.org">
          optinist-support@araya.org
        </ExternalLink>
      </P>
    </LegalPageLayout>
  )
}

export default Terms
