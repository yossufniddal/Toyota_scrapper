import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "محرك أسعار السيارات السعودي",
  description: "متوسط الأسعار لكل فئة من ٥ مصادر — تويوتا/لكزس + حراج + سيارة + موتوري + يلا موتور",
};

// Set theme class on <html> *before* React hydrates, so there's no flash.
const themeBootstrap = `
try {
  var t = localStorage.getItem('theme');
  if (!t) t = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  if (t === 'dark') document.documentElement.classList.add('dark');
} catch (e) {}
`;

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="ar" dir="rtl">
      <head>
        <script dangerouslySetInnerHTML={{ __html: themeBootstrap }} />
      </head>
      <body className="antialiased bg-white text-gray-900 dark:bg-slate-950 dark:text-gray-100">
        {children}
      </body>
    </html>
  );
}
