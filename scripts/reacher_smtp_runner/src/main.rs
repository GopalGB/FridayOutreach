use check_if_email_exists::{
    check_email, CheckEmailInput, CheckEmailInputBuilder, CheckEmailOutput, Reachable,
    smtp::verif_method::{
        EverythingElseVerifMethod, GmailVerifMethod, HotmailB2BVerifMethod,
        HotmailB2CVerifMethod, MimecastVerifMethod, ProofpointVerifMethod,
        VerifMethod, VerifMethodSmtpConfig, YahooVerifMethod,
    },
};
use serde::Serialize;
use std::{collections::HashMap, env, process::ExitCode, time::Duration};

const FROM_EMAIL: &str = "lead-collector-check@example.invalid";
const HELLO_NAME: &str = "lead-collector.invalid";

#[derive(Serialize)]
struct NativeResponse {
    version: u8,
    input: String,
    status: &'static str,
    #[serde(rename = "failureCategory", skip_serializing_if = "Option::is_none")]
    failure_category: Option<&'static str>,
    #[serde(rename = "syntaxValid")]
    syntax_valid: bool,
    #[serde(rename = "mxAcceptsMail")]
    mx_accepts_mail: bool,
    #[serde(rename = "smtpDeliverable")]
    smtp_deliverable: bool,
    #[serde(rename = "catchAll")]
    catch_all: bool,
    disabled: bool,
    #[serde(rename = "fullInbox")]
    full_inbox: bool,
}

fn smtp_config() -> VerifMethodSmtpConfig {
    VerifMethodSmtpConfig {
        proxy: None,
        hello_name: HELLO_NAME.to_owned(),
        from_email: FROM_EMAIL.to_owned(),
        smtp_port: 25,
        smtp_timeout: Some(Duration::from_secs(10)),
        retries: 1,
    }
}

fn smtp_only_methods() -> VerifMethod {
    let config = smtp_config();
    VerifMethod {
        proxies: HashMap::new(),
        gmail: GmailVerifMethod::Smtp(config.clone()),
        hotmailb2b: HotmailB2BVerifMethod::Smtp(config.clone()),
        hotmailb2c: HotmailB2CVerifMethod::Smtp(config.clone()),
        mimecast: MimecastVerifMethod::Smtp(config.clone()),
        proofpoint: ProofpointVerifMethod::Smtp(config.clone()),
        yahoo: YahooVerifMethod::Smtp(config.clone()),
        everything_else: EverythingElseVerifMethod::Smtp(config),
    }
}

fn email_argument() -> Option<String> {
    let mut arguments = env::args().skip(1);
    match (arguments.next(), arguments.next(), arguments.next()) {
        (Some(flag), Some(email), None) if flag == "--email" && !email.is_empty() => Some(email),
        _ => None,
    }
}

fn build_input(email: String) -> Result<CheckEmailInput, ()> {
    CheckEmailInputBuilder::default()
        .to_email(email)
        .verif_method(smtp_only_methods())
        .check_gravatar(false)
        .haveibeenpwned_api_key(None)
        .backend_name("lead-collector-reacher-smtp".to_owned())
        .sentry_dsn(None)
        .build()
        .map_err(|_| ())
}

fn native_response(output: CheckEmailOutput) -> NativeResponse {
    let status = match output.is_reachable {
        Reachable::Safe => "safe",
        Reachable::Risky => "risky",
        Reachable::Invalid => "invalid",
        Reachable::Unknown => "unknown",
    };
    let mx_accepts_mail = output.mx.as_ref().map(|details| details.accepts_mail).unwrap_or(false);
    let smtp = output.smtp.as_ref().ok();
    let failure_category = failure_category(&output, status);
    NativeResponse {
        version: 1,
        input: output.input,
        status,
        failure_category,
        syntax_valid: output.syntax.is_valid_syntax,
        mx_accepts_mail,
        smtp_deliverable: smtp.map(|details| details.is_deliverable).unwrap_or(false),
        catch_all: smtp.map(|details| details.is_catch_all).unwrap_or(false),
        disabled: smtp.map(|details| details.is_disabled).unwrap_or(false),
        full_inbox: smtp.map(|details| details.has_full_inbox).unwrap_or(false),
    }
}

fn failure_category(output: &CheckEmailOutput, status: &str) -> Option<&'static str> {
    if status != "unknown" {
        None
    } else if output.mx.is_err() {
        Some("mx")
    } else if output.smtp.is_err() {
        Some("smtp")
    } else {
        None
    }
}

#[tokio::main]
async fn main() -> ExitCode {
    let Some(email) = email_argument() else {
        return ExitCode::from(2);
    };
    let input = match build_input(email) {
        Ok(input) => input,
        Err(_) => return ExitCode::from(2),
    };
    let output = native_response(check_email(&input).await);
    match serde_json::to_string(&output) {
        Ok(value) => println!("{value}"),
        Err(_) => return ExitCode::from(1),
    }
    ExitCode::SUCCESS
}

#[cfg(test)]
mod tests {
    use super::*;

    fn assert_smtp_config(config: &VerifMethodSmtpConfig) {
        assert_eq!(config.proxy, None);
        assert_eq!(config.hello_name, HELLO_NAME);
        assert_eq!(config.from_email, FROM_EMAIL);
        assert_eq!(config.smtp_port, 25);
        assert_eq!(config.smtp_timeout, Some(Duration::from_secs(10)));
        assert_eq!(config.retries, 1);
    }

    fn configured_smtp(method: &YahooVerifMethod) -> &VerifMethodSmtpConfig {
        if let YahooVerifMethod::Smtp(config) = method {
            return config;
        }
        panic!("SMTP method required")
    }

    fn hotmail_b2c_smtp(method: &HotmailB2CVerifMethod) -> &VerifMethodSmtpConfig {
        if let HotmailB2CVerifMethod::Smtp(config) = method {
            return config;
        }
        panic!("SMTP method required")
    }

    #[test]
    fn all_provider_variants_use_smtp_without_a_proxy() {
        let methods = smtp_only_methods();
        assert!(methods.proxies.is_empty());
        let GmailVerifMethod::Smtp(gmail) = &methods.gmail;
        let HotmailB2BVerifMethod::Smtp(hotmail_b2b) = &methods.hotmailb2b;
        let MimecastVerifMethod::Smtp(mimecast) = &methods.mimecast;
        let ProofpointVerifMethod::Smtp(proofpoint) = &methods.proofpoint;
        let EverythingElseVerifMethod::Smtp(everything_else) = &methods.everything_else;
        for config in [
            gmail,
            hotmail_b2b,
            hotmail_b2c_smtp(&methods.hotmailb2c),
            mimecast,
            proofpoint,
            configured_smtp(&methods.yahoo),
            everything_else,
        ] {
            assert_smtp_config(config);
        }
    }

    #[test]
    fn builder_disables_gravatar_and_hibp() {
        let input = build_input("technical-check@company.co".to_owned()).expect("valid input");
        assert!(!input.check_gravatar);
        assert_eq!(input.haveibeenpwned_api_key, None);
        assert_smtp_config(configured_smtp(&input.verif_method.yahoo));
    }

    #[cfg(target_vendor = "apple")]
    #[test]
    fn apple_resolver_parser_skips_scoped_ipv6_nameservers() {
        let nameservers = check_if_email_exists::mx::local_nameservers(
            "nameserver fe80::1%en0\nnameserver 192.0.2.1\n",
        );
        assert_eq!(nameservers.len(), 1);
    }
}
