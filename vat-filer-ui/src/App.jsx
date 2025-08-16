import React, { useMemo, useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import {
  Building2,
  ChevronRight,
  UserPlus,
  Users,
  ShieldCheck,
  Mail,
  Lock,
  Phone,
  Briefcase,
  CheckCircle2,
  X,
} from "lucide-react";

const BG =
  "https://images.unsplash.com/photo-1520607162513-77705c0f0d4a?q=80&w=1600&auto=format&fit=crop"; // subtle office image

function Field({ label, children, hint, required }) {
  return (
    <div className="field">
      <label className="label">
        {label} {required && <span className="req">*</span>}
      </label>
      {children}
      {hint && <div className="hint">{hint}</div>}
    </div>
  );
}

function Input({ icon: Icon, ...props }) {
  return (
    <div className="input">
      {Icon && (
        <div className="inputIcon">
          <Icon size={16} />
        </div>
      )}
      <input {...props} />
    </div>
  );
}

function Checkbox({ label, ...props }) {
  return (
    <label className="check">
      <input type="checkbox" {...props} />
      <span>{label}</span>
    </label>
  );
}

function RecaptchaPlaceholder() {
  return (
    <div className="recaptcha">
      <span>I’m not a robot</span>
      <div className="recaptcha-badge">reCAPTCHA</div>
    </div>
  );
}

function Pill({ active, onClick, children }) {
  return (
    <button
      className={`pill ${active ? "active" : ""}`}
      type="button"
      onClick={onClick}
    >
      {children}
    </button>
  );
}

function RegisterForm({ mode }) {
  // mode: 'agent' | 'taxpayer'
  const [agree, setAgree] = useState(false);
  const [softwareVAT, setSoftwareVAT] = useState(false);
  const [softwareITSA, setSoftwareITSA] = useState(false);

  return (
    <motion.div
      key={mode}
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, y: -8 }}
    >
      <div className="card formCard">
        <div className="tabs">
          <span className={`tab ${mode === "agent" ? "on" : ""}`}>Tax Agent</span>
          <span className={`tab ${mode === "taxpayer" ? "on" : ""}`}>Taxpayer</span>
          <span className="tabLink">
            {mode === "agent" ? "Not an accountant?" : "Are you an accountant?"}
          </span>
        </div>

        <div className="grid2">
          <Field label="Email Address" required>
            <Input icon={Mail} placeholder="Please enter your email" />
          </Field>
          <Field label="Confirm Email Address" required>
            <Input icon={Mail} placeholder="Please confirm your email" />
          </Field>

          <Field
            label="Password"
            required
            hint={`Please don't use "&,<,>" in your password.`}
          >
            <Input
              icon={Lock}
              type="password"
              placeholder="Please enter a password"
            />
          </Field>
          <Field label="Confirm Password" required>
            <Input
              icon={Lock}
              type="password"
              placeholder="Please confirm the password"
            />
          </Field>

          <Field label="Contact Name" required>
            <Input placeholder="Please enter a contact name" />
          </Field>
          <Field label="Business Name" required>
            <Input icon={Briefcase} placeholder="Please enter a business name" />
          </Field>

          <Field label="Phone Number">
            <Input icon={Phone} placeholder="Please enter a phone number" />
          </Field>
        </div>

        <div className="soft">
          <div className="softTitle">Software</div>
          <div className="softGrid">
            <Checkbox
              label="VAT"
              checked={softwareVAT}
              onChange={(e) => setSoftwareVAT(e.target.checked)}
            />
            <Checkbox
              label="MTD Quarterly Income Tax"
              checked={softwareITSA}
              onChange={(e) => setSoftwareITSA(e.target.checked)}
            />
          </div>
        </div>

        <div className="agree">
          <Checkbox
            label={
              <>
                By ticking this box I/we agree to your{" "}
                <a href="#" onClick={(e) => e.preventDefault()}>
                  Terms of Service
                </a>
              </>
            }
            checked={agree}
            onChange={(e) => setAgree(e.target.checked)}
          />
        </div>

        <div className="recaptchaRow">
          <RecaptchaPlaceholder />
        </div>

        <div className="actionsRow">
          <button className="btn ghost">Cancel</button>
          <button className="btn primary" disabled={!agree}>
            Register
          </button>
        </div>
      </div>
    </motion.div>
  );
}

export default function App() {
  const [showModal, setShowModal] = useState(false);
  const [mode, setMode] = useState("taxpayer"); // default tab when form shows
  const [stage, setStage] = useState("landing"); // 'landing' | 'choose' | 'form'

  const year = useMemo(() => new Date().getFullYear(), []);

  return (
    <div className="app">
      {/* Background layers */}
      <div className="bgWrap">
        <img src={BG} alt="" />
        <div className="overlay" />
        <div className="gradient" />
      </div>

      <header className="container header">
        <div className="brand">
          <div className="logo">
            <Building2 size={18} />
          </div>
          <span>PTP Associates</span>
          <span className="badge">MTD VAT</span>
        </div>
        <div className="hdrRight">
          <ShieldCheck size={18} />
          <span>Secure signup</span>
        </div>
      </header>

      <main className="container main">
        <motion.div
          className="hero"
          initial={{ opacity: 0, y: 10 }}
          animate={{ opacity: 1, y: 0 }}
        >
          <h1>Professional accounting & tax services</h1>
          <p>
            Create an account to get started. Choose <b>Taxpayer</b> or{" "}
            <b>Tax Agent</b>, then complete your details below.
          </p>

          {stage === "landing" && (
            <div className="ctaRow">
              <button
                className="btn primary"
                onClick={() => {
                  setShowModal(true);
                  setStage("choose");
                }}
              >
                Create account <ChevronRight size={18} />
              </button>
            </div>
          )}
        </motion.div>

        {/* Modal: choose account type */}
        <AnimatePresence>
          {showModal && stage === "choose" && (
            <motion.div
              className="modalBackdrop"
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
            >
              <motion.div
                className="modal"
                initial={{ scale: 0.96, opacity: 0 }}
                animate={{ scale: 1, opacity: 1 }}
                exit={{ scale: 0.96, opacity: 0 }}
              >
                <div className="modalHdr">
                  <h3>Register</h3>
                  <button className="iconBtn" onClick={() => setShowModal(false)}>
                    <X size={16} />
                  </button>
                </div>
                <div className="modalBody">
                  <p className="modalLead">I need a:</p>
                  <div className="chooseGrid">
                    <button
                      className="chooseBtn blue"
                      onClick={() => {
                        setMode("taxpayer");
                        setShowModal(false);
                        setStage("form");
                      }}
                    >
                      <UserPlus size={18} />
                      Taxpayer Account
                    </button>
                    <button
                      className="chooseBtn green"
                      onClick={() => {
                        setMode("agent");
                        setShowModal(false);
                        setStage("form");
                      }}
                    >
                      <Users size={18} />
                      Tax Agent Account
                    </button>
                  </div>
                </div>
              </motion.div>
            </motion.div>
          )}
        </AnimatePresence>

        {/* Register form */}
        {stage === "form" && (
          <div className="formWrap">
            <div className="formTop">
              <div className="pillRow">
                <Pill active={mode === "taxpayer"} onClick={() => setMode("taxpayer")}>
                  <UserPlus size={15} />
                  Taxpayer
                </Pill>
                <Pill active={mode === "agent"} onClick={() => setMode("agent")}>
                  <Users size={15} />
                  Tax Agent
                </Pill>
              </div>
              <div className="topNote">
                <CheckCircle2 size={16} /> Fill in the details to create your{" "}
                {mode === "agent" ? "agent" : "taxpayer"} account
              </div>
            </div>

            <RegisterForm mode={mode} />
          </div>
        )}
      </main>

      <footer className="container footer">
        © {year} PTP Associates · Privacy · Terms
      </footer>
    </div>
  );
}
