import "./globals.css";

export const metadata = {
  title: "Nexus Layer",
  description: "Grounded fact extraction and cross-document reconciliation for PDFs.",
};

export default function RootLayout({ children }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
