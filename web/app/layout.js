import "./globals.css";

export const metadata = { title: "Fact Knowledge Layer" };

export default function RootLayout({ children }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
